"""
LoopEngine — 统一执行引擎。

接管通知、记忆读写、目标判断、动态重调度。
Loop 实现只负责业务逻辑，report() 只返回字符串。
"""

import logging
from dataclasses import dataclass

from engine.context import RunContext, ToolRegistry
from engine.memory import MemoryStore

log = logging.getLogger(__name__)

MAX_RETRIES = 2


@dataclass
class RunResult:
    summary: str
    result: dict
    success: bool = True


class LoopEngine:

    def __init__(self, memory_store: MemoryStore | None = None,
                 scheduler=None, notifier=None):
        self.memory = memory_store or MemoryStore()
        self._scheduler = scheduler   # 延迟注入，避免循环依赖
        self._notifier = notifier

    # ── 主入口 ───────────────────────────────────────────

    def run(self, loop, goal: dict) -> RunResult:
        loop_name = loop.name
        log.info(f"[engine] 开始执行 loop={loop_name} goal={goal['id']}")

        # 1. 加载记忆
        memory = self.memory.load(loop_name)

        # 2. 构建 RunContext，注入工具
        ctx = RunContext(
            goal=goal,
            memory=memory,
            tools=ToolRegistry.build(getattr(loop, "required_tools", [])),
        )

        # 3. 执行五阶段
        result, summary = self._run_phases(loop, goal, ctx)

        # 4. 统一通知（从 report 里剥离）
        self._notify(loop, summary)

        # 5. 沉淀记忆
        new_memory = loop.extract_memory(result, memory)
        self.memory.save(loop_name, new_memory)

        # 6. 目标驱动：判断是否需要重触发
        self._maybe_reschedule(loop, goal, result, new_memory)

        log.info(f"[engine] 完成 loop={loop_name} | {summary}")
        return RunResult(summary=summary, result=result)

    # ── 内部阶段执行 ─────────────────────────────────────

    def _run_phases(self, loop, goal: dict, ctx: RunContext) -> tuple[dict, str]:
        # plan
        try:
            context = loop.plan(goal, ctx)
        except Exception as e:
            msg = f"规划阶段失败: {e}"
            log.error(f"[engine:{loop.name}] {msg}")
            return {}, msg

        # execute
        try:
            result = loop.execute(context, ctx)
        except Exception as e:
            msg = f"执行阶段失败: {e}"
            log.error(f"[engine:{loop.name}] {msg}")
            return {}, msg

        # verify + fix
        for attempt in range(MAX_RETRIES + 1):
            try:
                ok, issues = loop.verify(result)
            except Exception as e:
                log.warning(f"[engine:{loop.name}] 验证异常（跳过）: {e}")
                break
            if ok:
                break
            log.info(f"[engine:{loop.name}] 验证失败 attempt={attempt + 1}: {issues}")
            if attempt < MAX_RETRIES:
                try:
                    result = loop.fix(result, issues, ctx)
                except Exception as e:
                    log.error(f"[engine:{loop.name}] 修复失败: {e}")
                    break
            else:
                log.warning(f"[engine:{loop.name}] 重试耗尽，使用最后结果")

        summary = loop.report(result)
        return result, summary

    # ── 通知 ─────────────────────────────────────────────

    def _notify(self, loop, summary: str) -> None:
        # 如果 Loop 已迁移（report 不自己发通知），由 Engine 统一发
        if not getattr(loop, "_legacy_notify", False):
            return  # 已迁移的 Loop：report() 里不发通知，这里也先不发（由 Loop 自行控制过渡期）
        # 未来：self._notifier.send(summary)

    # ── 目标驱动重调度 ───────────────────────────────────

    def _maybe_reschedule(self, loop, goal: dict, result: dict, memory: dict) -> None:
        if goal.get("trigger_mode") != "goal":
            return
        if loop.is_goal_met(result, memory):
            log.info(f"[engine] 目标已达成，停止重调度 goal={goal['id']}")
            return
        delay = loop.next_trigger(result)
        if delay and self._scheduler:
            self._scheduler.reschedule(goal["id"], delay)
            log.info(f"[engine] 目标未达成，{delay} 后重触发 goal={goal['id']}")


# 模块级单例，供 scheduler 使用
_engine: LoopEngine | None = None


def get_engine() -> LoopEngine:
    global _engine
    if _engine is None:
        _engine = LoopEngine()
    return _engine
