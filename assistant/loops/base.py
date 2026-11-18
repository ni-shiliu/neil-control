"""
Loop 基类。

定义标准六阶段流程：规划 → 执行 → 验证 → 修复 → 汇报
子类覆写 plan / execute / verify / fix / report 五个方法即可。

新增钩子（Loop Engineering 扩展）：
  - is_goal_met()    目标模式：判断本次是否达成目标
  - next_trigger()   目标模式：未达成时返回下次触发间隔
  - extract_memory() 持久记忆：把本次结果沉淀到跨 run 记忆
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from engine.context import RunContext

log = logging.getLogger(__name__)

MAX_RETRIES = 2

CommitEffects = Callable[["BaseLoop", "RunContext", dict, dict], dict]


@dataclass
class LoopRunOnceResult:
    result: dict
    summary: str
    phase_data: dict
    success: bool


class BaseLoop(ABC):

    name: str = "base"
    description: str = ""
    required_tools: list[str] = []   # 声明依赖的工具，Engine 按需注入
    supported_trigger_modes: tuple[str, ...] = ("cron", "goal")
    use_loop_doc: bool = False

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.description:
            raise TypeError(f"{cls.__name__} 必须声明 description 类属性")

    # ── 模板方法入口 ─────────────────────────────────────

    def run_once(
        self,
        goal: dict,
        ctx: "RunContext",
        *,
        commit_effects: CommitEffects | None = None,
        max_retries: int = MAX_RETRIES,
    ) -> LoopRunOnceResult:
        """
        模板方法：固定单次 loop 闭环，子类只覆写各阶段钩子。

        Harness 不属于这个骨架；如果某个 loop 需要 AI 多轮工具调用，
        应在 execute/fix 内显式调用 HarnessRunner。
        """
        phase_data = {
            "plan": {"ok": False, "error": None},
            "execute": {"ok": False, "error": None},
            "effects": {"planned": [], "attempts": [], "dry_run": bool(goal.get("dry_run", False))},
            "verify": {"attempts": []},
            "report": {"ok": False, "error": None},
            "runtime": {"type": "loop_template"},
        }

        try:
            context = self.plan(goal, ctx)
            phase_data["plan"]["ok"] = True
            phase_data["plan"]["context_keys"] = sorted(context.keys())
        except Exception as e:
            msg = f"规划阶段失败: {e}"
            log.error(f"[{self.name}] {msg}")
            phase_data["plan"]["error"] = str(e)
            return LoopRunOnceResult({}, msg, phase_data, False)

        try:
            result = self.execute(context, ctx)
            phase_data["execute"]["mode"] = "loop_template"
            phase_data["execute"]["ok"] = True
            phase_data["execute"]["result_keys"] = sorted(result.keys())
        except Exception as e:
            msg = f"执行阶段失败: {e}"
            log.error(f"[{self.name}] {msg}")
            phase_data["execute"]["error"] = str(e)
            return LoopRunOnceResult({}, msg, phase_data, False)

        result = self._settle_effects(ctx, result, phase_data, commit_effects)

        for attempt in range(max_retries + 1):
            try:
                ok, issues = self.verify(result)
                phase_data["verify"]["attempts"].append({
                    "attempt": attempt + 1,
                    "ok": ok,
                    "issues": issues,
                })
            except Exception as e:
                log.warning(f"[{self.name}] 验证异常（跳过）: {e}")
                phase_data["verify"]["attempts"].append({
                    "attempt": attempt + 1,
                    "ok": True,
                    "issues": f"verify skipped: {e}",
                })
                break
            if ok:
                break
            log.info(f"[{self.name}] 验证失败 attempt={attempt + 1}: {issues}")
            if attempt < max_retries:
                try:
                    result = self.fix(result, issues, ctx)
                    result = self._settle_effects(ctx, result, phase_data, commit_effects)
                    phase_data["verify"]["attempts"][-1]["fixed"] = True
                except Exception as e:
                    log.error(f"[{self.name}] 修复失败: {e}")
                    phase_data["verify"]["attempts"][-1]["fix_error"] = str(e)
                    break
            else:
                log.warning(f"[{self.name}] 重试耗尽，使用最后结果")

        try:
            summary = self.report(result)
            phase_data["report"]["ok"] = True
        except Exception as e:
            summary = f"汇报阶段失败: {e}"
            phase_data["report"]["error"] = str(e)
            return LoopRunOnceResult(result, summary, phase_data, False)
        return LoopRunOnceResult(result, summary, phase_data, True)

    def _settle_effects(
        self,
        ctx: "RunContext",
        result: dict,
        phase_data: dict,
        commit_effects: CommitEffects | None,
    ) -> dict:
        if commit_effects:
            result = commit_effects(self, ctx, result, phase_data)
        return self.after_effects(result, ctx)

    # ── 子类必须实现 ─────────────────────────────────────

    @abstractmethod
    def plan(self, goal: dict, ctx: "RunContext | None" = None) -> dict:
        """收集执行所需上下文，返回 context dict。"""

    @abstractmethod
    def execute(self, context: dict, ctx: "RunContext | None" = None) -> dict:
        """执行核心业务逻辑，返回 result dict。"""

    @abstractmethod
    def verify(self, result: dict) -> tuple[bool, str]:
        """验证结果，返回 (是否通过, 问题描述)。"""

    @abstractmethod
    def fix(self, result: dict, issues: str, ctx: "RunContext | None" = None) -> dict:
        """根据问题修复结果，返回修复后的 result dict。"""

    @abstractmethod
    def report(self, result: dict) -> str:
        """生成结果摘要字符串（只返回字符串，不发通知）。"""

    # ── Loop Engineering 扩展钩子（子类可选覆写）────────

    def build_notifications(
        self,
        result: dict,
        summary: str,
        ctx: "RunContext | None" = None,
    ) -> list[dict]:
        """生成通知请求，由 Engine 统一分发。默认不发任何通知。"""
        return []

    def after_effects(self, result: dict, ctx: "RunContext | None" = None) -> dict:
        """Engine 提交副作用后调用，允许 Loop 刷新衍生状态。"""
        return result

    def is_goal_met(self, result: dict, memory: dict) -> bool:
        """目标模式：判断本次执行是否达成目标。
        默认返回 True（即 cron 模式，每次执行后视为完成）。
        """
        return True

    def next_trigger(self, result: dict) -> timedelta | None:
        """目标模式：未达成时返回下次触发间隔；返回 None 表示不再重试。
        默认返回 None（不自动重触发）。
        """
        return None

    def extract_memory(self, result: dict, old_memory: dict) -> dict:
        """持久记忆：把本次执行结果沉淀为跨 run 记忆，Engine 会保存返回值。
        默认不更新记忆（返回原记忆）。
        """
        return old_memory

    def extract_goal_memory(self, result: dict, old_memory: dict) -> dict:
        """goal 级记忆：沉淀某个具体 goal 的短期状态。默认原样返回。"""
        return old_memory
