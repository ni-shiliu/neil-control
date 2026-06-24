"""
LoopEngine — Loop Engineering runtime。

LoopEngine 负责目标型任务的一次运行生命周期：
上下文、调用 loop 模板方法、effects、通知、记忆、运行记录、目标判断、动态重调度。

注意：这里不是 Harness。
Harness 的核心是 AI -> tool_use -> tool_result 的 agentic 循环；
LoopEngine 的核心是 goal -> plan -> execute -> verify -> memory/reschedule 的目标推进循环。
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from time import perf_counter
import os
import tempfile
import uuid

from engine.context import RunContext
from engine.effects import Effect, EffectHistoryStore
from engine.memory import MemoryStore
from engine.records import RunRecord, RunRecorder
from engine.runtime_context import build_loop_run_context

log = logging.getLogger(__name__)

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

    # ── 主入口 ───────────────────────────────────────────

    def run(self, loop, goal: dict) -> RunResult:
        started_at = datetime.now(timezone.utc)
        started_clock = perf_counter()
        run_id = uuid.uuid4().hex[:12]
        loop_name = loop.name
        log.info(f"[engine] 开始执行 loop={loop_name} goal={goal['id']} run={run_id}")

        # 1. 构建 run 级上下文。
        # 这一步已经抽到 runtime_context 中，避免 loop / chat 各自重复装配 memory / docs / recent records / tools。
        ctx = build_loop_run_context(
            memory_store=self.memory,
            recorder=self.recorder,
            loop=loop,
            goal=goal,
            run_id=run_id,
        )
        dry_run = bool(goal.get("dry_run", False))

        memory = ctx.memory
        goal_memory = ctx.goal_memory

        # 2. 调用 BaseLoop 模板方法推进单次 loop 闭环。
        # 如果某个 loop 内部需要 AI 多轮工具调用，应由该 loop 在 execute/fix 中显式使用 HarnessRunner。
        result, summary, phase_data, success = self._execute_loop_template(loop, goal, ctx)

        notifications: list[dict] = []
        new_memory = memory
        new_goal_memory = goal_memory
        next_trigger_in_seconds = None

        if success:
            # 3. 统一通知（从 report 里剥离）
            notifications = self._notify(loop, result, summary, ctx)

            # 4. 沉淀记忆
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

            # 5. 目标驱动：判断是否需要重触发
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

    # ── Loop 模板方法 ─────────────────────────────────────

    def _execute_loop_template(self, loop, goal: dict, ctx: RunContext) -> tuple[dict, str, dict, bool]:
        run_result = loop.run_once(
            goal,
            ctx,
            commit_effects=self._commit_effects,
        )
        return (
            run_result.result,
            run_result.summary,
            run_result.phase_data,
            run_result.success,
        )

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
