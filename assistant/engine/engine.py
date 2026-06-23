"""
LoopEngine — 统一执行引擎。

接管通知、记忆读写、运行记录、目标判断、动态重调度。
Loop 实现只负责业务逻辑，report() 只返回字符串。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
import os
import tempfile
import uuid
from pathlib import Path

from engine.context import RunContext, ToolRegistry
from engine.effects import Effect, EffectHistoryStore
from engine.memory import MemoryStore
from engine.records import RunRecord, RunRecorder

log = logging.getLogger(__name__)

MAX_RETRIES = 2
RECENT_RUNS_LIMIT = 5


@dataclass
class RunResult:
    summary: str
    result: dict
    record: RunRecord
    success: bool = True


class LoopEngine:

    def __init__(self, memory_store: MemoryStore | None = None,
                 scheduler=None, notifier=None, recorder: RunRecorder | None = None):
        self.memory = memory_store or MemoryStore()
        self._scheduler = scheduler   # 延迟注入，避免循环依赖
        self._notifier = notifier
        self.recorder = recorder or RunRecorder()
        self.effect_history = EffectHistoryStore()

    @staticmethod
    def _read_optional_doc(path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception as e:
            log.warning(f"[engine] 文档加载失败 path={path.name}: {e}")
            return ""

    # ── 主入口 ───────────────────────────────────────────

    def run(self, loop, goal: dict) -> RunResult:
        started_at = datetime.now(timezone.utc)
        started_clock = perf_counter()
        run_id = uuid.uuid4().hex[:12]
        loop_name = loop.name
        log.info(f"[engine] 开始执行 loop={loop_name} goal={goal['id']} run={run_id}")

        # 1. 加载记忆
        memory = self.memory.load_loop_memory(loop_name)
        goal_memory = self.memory.load_goal_memory(goal["id"])
        assistant_dir = Path(__file__).resolve().parent.parent
        runtime_doc = self._read_optional_doc(assistant_dir / "RUNTIME.md")
        loop_doc = self._read_optional_doc(assistant_dir / "loops" / f"{loop_name}.md")
        recent_runs = {
            "goal_recent_runs": self.recorder.list_recent_by_goal(goal["id"], limit=RECENT_RUNS_LIMIT),
            "loop_recent_runs": self.recorder.list_recent_by_loop(loop_name, limit=RECENT_RUNS_LIMIT),
        }

        # 2. 构建 RunContext，注入工具
        ctx = RunContext(
            run_id=run_id,
            goal=goal,
            memory=memory,
            goal_memory=goal_memory,
            recent_runs=recent_runs,
            runtime_doc=runtime_doc,
            loop_doc=loop_doc,
            tools=ToolRegistry.build(getattr(loop, "required_tools", [])),
        )
        dry_run = bool(goal.get("dry_run", False))

        # 3. 执行五阶段
        result, summary, phase_data, success = self._run_phases(loop, goal, ctx)

        # 4. 统一通知（从 report 里剥离）
        notifications = self._notify(loop, result, summary, ctx)

        # 5. 沉淀记忆
        new_memory = loop.extract_memory(result, memory)
        self.memory.save_loop_memory(loop_name, new_memory)
        new_goal_memory = self._extract_goal_memory(
            loop,
            result,
            goal_memory,
            summary=summary,
            success=success,
            run_id=run_id,
        )
        self.memory.save_goal_memory(goal["id"], new_goal_memory)

        # 6. 目标驱动：判断是否需要重触发
        next_trigger_in_seconds = self._maybe_reschedule(loop, goal, result, new_memory)

        ended_at = datetime.now(timezone.utc)
        duration_ms = int((perf_counter() - started_clock) * 1000)
        record = RunRecord(
            run_id=run_id,
            goal_id=goal["id"],
            loop_name=loop_name,
            status="success" if success else "failed",
            trigger_mode=goal.get("trigger_mode"),
            started_at=started_at.strftime("%Y%m%dT%H%M%SZ"),
            ended_at=ended_at.strftime("%Y%m%dT%H%M%SZ"),
            duration_ms=duration_ms,
            summary=summary,
            result=result,
            phase_data=phase_data,
            planned_effects=phase_data["effects"]["planned"],
            committed_effects=phase_data["effects"]["attempts"],
            dry_run=dry_run,
            memory_before=memory,
            memory_after=new_memory,
            goal_memory_before=goal_memory,
            goal_memory_after=new_goal_memory,
            notifications=notifications,
            next_trigger_in_seconds=next_trigger_in_seconds,
            error=None if success else summary,
        )
        record_path = self.recorder.save(record)

        log.info(f"[engine] 完成 loop={loop_name} | {summary} | record={record_path.name}")
        return RunResult(summary=summary, result=result, record=record, success=success)

    # ── 内部阶段执行 ─────────────────────────────────────

    def _run_phases(self, loop, goal: dict, ctx: RunContext) -> tuple[dict, str, dict, bool]:
        phase_data = {
            "plan": {"ok": False, "error": None},
            "execute": {"ok": False, "error": None},
            "effects": {"planned": [], "attempts": [], "dry_run": bool(goal.get("dry_run", False))},
            "verify": {"attempts": []},
            "report": {"ok": False, "error": None},
        }

        # plan
        try:
            context = loop.plan(goal, ctx)
            phase_data["plan"]["ok"] = True
            phase_data["plan"]["context_keys"] = sorted(context.keys())
        except Exception as e:
            msg = f"规划阶段失败: {e}"
            log.error(f"[engine:{loop.name}] {msg}")
            phase_data["plan"]["error"] = str(e)
            return {}, msg, phase_data, False

        # execute
        try:
            result = loop.execute(context, ctx)
            phase_data["execute"]["ok"] = True
            phase_data["execute"]["result_keys"] = sorted(result.keys())
        except Exception as e:
            msg = f"执行阶段失败: {e}"
            log.error(f"[engine:{loop.name}] {msg}")
            phase_data["execute"]["error"] = str(e)
            return {}, msg, phase_data, False

        result = self._commit_effects(loop, ctx, result, phase_data)
        result = loop.after_effects(result, ctx)

        # verify + fix
        for attempt in range(MAX_RETRIES + 1):
            try:
                ok, issues = loop.verify(result)
                phase_data["verify"]["attempts"].append({
                    "attempt": attempt + 1,
                    "ok": ok,
                    "issues": issues,
                })
            except Exception as e:
                log.warning(f"[engine:{loop.name}] 验证异常（跳过）: {e}")
                phase_data["verify"]["attempts"].append({
                    "attempt": attempt + 1,
                    "ok": True,
                    "issues": f"verify skipped: {e}",
                })
                break
            if ok:
                break
            log.info(f"[engine:{loop.name}] 验证失败 attempt={attempt + 1}: {issues}")
            if attempt < MAX_RETRIES:
                try:
                    result = loop.fix(result, issues, ctx)
                    result = self._commit_effects(loop, ctx, result, phase_data)
                    result = loop.after_effects(result, ctx)
                    phase_data["verify"]["attempts"][-1]["fixed"] = True
                except Exception as e:
                    log.error(f"[engine:{loop.name}] 修复失败: {e}")
                    phase_data["verify"]["attempts"][-1]["fix_error"] = str(e)
                    break
            else:
                log.warning(f"[engine:{loop.name}] 重试耗尽，使用最后结果")

        try:
            summary = loop.report(result)
            phase_data["report"]["ok"] = True
        except Exception as e:
            summary = f"汇报阶段失败: {e}"
            phase_data["report"]["error"] = str(e)
            return result, summary, phase_data, False
        return result, summary, phase_data, True

    # ── 通知 ─────────────────────────────────────────────

    def _notify(self, loop, result: dict, summary: str, ctx: RunContext) -> list[dict]:
        notifications = []
        try:
            requests = loop.build_notifications(result, summary, ctx)
        except Exception as e:
            log.error(f"[engine:{loop.name}] 构建通知失败: {e}")
            return [{"ok": False, "channel": "build", "error": str(e)}]

        for req in requests:
            channel = req.get("channel", "")
            try:
                if channel == "telegram_message":
                    if not ctx.tools.telegram:
                        raise RuntimeError("telegram tool 未注入")
                    ctx.tools.telegram.send(
                        req["text"],
                        parse_mode=req.get("parse_mode", "HTML"),
                    )
                elif channel == "telegram_document":
                    if not ctx.tools.telegram:
                        raise RuntimeError("telegram tool 未注入")
                    self._send_telegram_document(ctx, req)
                else:
                    raise ValueError(f"未知通知渠道: {channel}")
                notifications.append({"ok": True, "channel": channel})
            except Exception as e:
                log.error(f"[engine:{loop.name}] 通知失败 channel={channel}: {e}")
                notifications.append({"ok": False, "channel": channel, "error": str(e)})
        return notifications

    @staticmethod
    def _extract_goal_memory(
        loop,
        result: dict,
        goal_memory: dict,
        *,
        summary: str,
        success: bool,
        run_id: str,
    ) -> dict:
        from loops.base import BaseLoop

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_runs = list(goal_memory.get("recent_runs", []))
        run_entry = {
            "run_id": run_id,
            "status": "success" if success else "failed",
            "summary": summary,
            "updated_at": now,
        }
        recent_runs.append(run_entry)
        recent_failures = list(goal_memory.get("recent_failures", []))
        if not success:
            recent_failures.append(run_entry)
        memory = {
            **goal_memory,
            "last_run_id": run_id,
            "last_status": "success" if success else "failed",
            "last_summary": summary,
            "last_result_keys": sorted(result.keys()),
            "last_updated_at": now,
            "recent_runs": recent_runs[-RECENT_RUNS_LIMIT:],
            "recent_failures": recent_failures[-RECENT_RUNS_LIMIT:],
        }
        if loop.__class__.extract_goal_memory is BaseLoop.extract_goal_memory:
            return memory
        try:
            custom = loop.extract_goal_memory(result, goal_memory)
        except Exception:
            return memory
        return {**memory, **custom}

    def _commit_effects(self, loop, ctx: RunContext, result: dict, phase_data: dict) -> dict:
        effects = ctx.effects.drain()
        if not effects:
            return result

        for effect in effects:
            phase_data["effects"]["planned"].append({
                "type": effect.effect_type,
                "payload_keys": sorted(effect.payload.keys()),
                "meta_keys": sorted(effect.meta.keys()),
                "idempotency_key": effect.resolved_idempotency_key(),
                "dry_run": bool(ctx.goal.get("dry_run", False) or effect.meta.get("dry_run", False)),
            })
            effect_log = {
                "type": effect.effect_type,
                "payload_keys": sorted(effect.payload.keys()),
                "meta_keys": sorted(effect.meta.keys()),
                "idempotency_key": effect.resolved_idempotency_key(),
                "ok": False,
                "status": "pending",
                "error": None,
            }
            dry_run = bool(ctx.goal.get("dry_run", False) or effect.meta.get("dry_run", False))
            if dry_run:
                effect_log["ok"] = True
                effect_log["status"] = "dry_run"
                phase_data["effects"]["attempts"].append(effect_log)
                continue

            effect_key = effect.resolved_idempotency_key()
            if self.effect_history.seen(effect_key):
                effect_log["ok"] = True
                effect_log["status"] = "duplicate_skipped"
                phase_data["effects"]["attempts"].append(effect_log)
                continue

            try:
                self._apply_effect(ctx, effect)
                self._apply_effect_success(result, effect)
                effect_log["ok"] = True
                effect_log["status"] = "committed"
                self.effect_history.mark_seen(effect_key, {
                    "effect_type": effect.effect_type,
                    "goal_id": ctx.goal.get("id"),
                    "committed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            except Exception as e:
                log.error(f"[engine:{loop.name}] effect 提交失败 type={effect.effect_type}: {e}")
                self._apply_effect_failure(result, effect, str(e))
                effect_log["status"] = "failed"
                effect_log["error"] = str(e)
            phase_data["effects"]["attempts"].append(effect_log)
        return result

    def _apply_effect(self, ctx: RunContext, effect: Effect) -> None:
        payload = effect.payload
        if effect.effect_type == "mark_read":
            if not ctx.tools.imap:
                raise RuntimeError("imap tool 未注入")
            ctx.tools.imap.mark_read(payload["uid"])
            return
        if effect.effect_type == "send_email_and_mark_read":
            if not ctx.tools.smtp or not ctx.tools.imap:
                raise RuntimeError("smtp 或 imap tool 未注入")
            ctx.tools.smtp.send(payload["to"], payload["subject"], payload["body"])
            ctx.tools.imap.mark_read(payload["uid"])
            return
        if effect.effect_type == "save_draft_and_mark_read":
            if not ctx.tools.imap:
                raise RuntimeError("imap tool 未注入")
            ctx.tools.imap.save_draft_and_mark_read(
                payload["uid"],
                payload["to"],
                payload["subject"],
                payload["body"],
            )
            return
        if effect.effect_type == "send_telegram_document":
            if not ctx.tools.telegram:
                raise RuntimeError("telegram tool 未注入")
            self._send_telegram_document(ctx, payload)
            return
        raise ValueError(f"未知 effect 类型: {effect.effect_type}")

    @staticmethod
    def _apply_effect_success(result: dict, effect: Effect) -> None:
        bucket = effect.meta.get("success_bucket")
        item = effect.meta.get("success_item")
        if bucket and item is not None:
            result.setdefault(bucket, []).append(item)

    @staticmethod
    def _apply_effect_failure(result: dict, effect: Effect, error: str) -> None:
        item = dict(effect.meta.get("failure_item", {}))
        if not item:
            item = dict(effect.payload)
        item["error"] = error
        item.setdefault("category", "effect_commit")
        result.setdefault("failed", []).append(item)

    def _send_telegram_document(self, ctx: RunContext, request: dict) -> None:
        file_path = request.get("file_path")
        if file_path:
            ctx.tools.telegram.send_document(file_path, caption=request.get("caption", ""))
            return

        content = request.get("content")
        if content is None:
            raise ValueError("telegram_document 缺少 file_path 或 content")

        suffix = request.get("suffix", ".txt")
        prefix = request.get("prefix", "loop_")
        with tempfile.NamedTemporaryFile(
            suffix=suffix,
            prefix=prefix,
            delete=False,
            mode="w",
            encoding=request.get("encoding", "utf-8"),
        ) as f:
            f.write(content)
            tmp_path = f.name

        try:
            ctx.tools.telegram.send_document(tmp_path, caption=request.get("caption", ""))
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ── 目标驱动重调度 ───────────────────────────────────

    def _maybe_reschedule(self, loop, goal: dict, result: dict, memory: dict) -> int | None:
        if goal.get("trigger_mode") != "goal":
            return None
        if loop.is_goal_met(result, memory):
            log.info(f"[engine] 目标已达成，停止重调度 goal={goal['id']}")
            return None
        delay = loop.next_trigger(result)
        if delay and self._scheduler:
            self._scheduler.reschedule(goal["id"], delay)
            log.info(f"[engine] 目标未达成，{delay} 后重触发 goal={goal['id']}")
            return int(delay.total_seconds())
        return None


# 模块级单例，供 scheduler 使用
_engine: LoopEngine | None = None


def get_engine() -> LoopEngine:
    global _engine
    if _engine is None:
        _engine = LoopEngine()
    return _engine
