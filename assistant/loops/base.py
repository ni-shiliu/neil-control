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
from datetime import timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.context import RunContext

log = logging.getLogger(__name__)

MAX_RETRIES = 2


class BaseLoop(ABC):

    name: str = "base"
    description: str = ""
    required_tools: list[str] = []   # 声明依赖的工具，Engine 按需注入

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if not cls.description:
            raise TypeError(f"{cls.__name__} 必须声明 description 类属性")

    # ── 主执行入口（兼容旧调用，新代码走 LoopEngine.run）───

    def run(self, goal: dict) -> str:
        """直接执行，不经过 Engine（兼容旧 scheduler 和测试）。"""
        log.info(f"[{self.name}] 开始执行 | goal={goal['id']}")

        try:
            context = self.plan(goal)
        except Exception as e:
            msg = f"规划阶段失败: {e}"
            log.error(f"[{self.name}] {msg}")
            return msg

        try:
            result = self.execute(context)
        except Exception as e:
            msg = f"执行阶段失败: {e}"
            log.error(f"[{self.name}] {msg}")
            return msg

        for attempt in range(MAX_RETRIES + 1):
            try:
                ok, issues = self.verify(result)
            except Exception as e:
                log.warning(f"[{self.name}] 验证异常（跳过）: {e}")
                break
            if ok:
                break
            log.info(f"[{self.name}] 验证失败 attempt={attempt + 1}: {issues}")
            if attempt < MAX_RETRIES:
                try:
                    result = self.fix(result, issues)
                except Exception as e:
                    log.error(f"[{self.name}] 修复失败: {e}")
                    break
            else:
                log.warning(f"[{self.name}] 重试耗尽，使用最后结果")

        summary = self.report(result)
        log.info(f"[{self.name}] 完成 | {summary}")
        return summary

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
