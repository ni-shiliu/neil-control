"""
Loop 基类。

定义标准六阶段流程：规划 → 执行 → 验证 → 修复 → 汇报
子类覆写 plan / execute / verify / fix 四个方法即可，report 由基类统一处理。
"""

import logging
from abc import ABC, abstractmethod

log = logging.getLogger(__name__)

MAX_RETRIES = 2


class BaseLoop(ABC):

    name: str = "base"  # 子类声明自己的名称，用于日志和通知
    description: str = ""  # 子类必须声明，描述"我能做什么场景"，供 Claude 解析自然语言时挑选

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        # 跳过 BaseLoop 自身的检查
        if cls.__dict__.get("description", "") == "" and "description" not in cls.__dict__:
            pass
        if not cls.description:
            raise TypeError(f"{cls.__name__} 必须声明 description 类属性")

    def run(self, goal: dict) -> str:
        """
        执行完整 Loop，返回结果摘要字符串。
        goal 是 goals.json 中的一条记录。
        """
        log.info(f"[{self.name}] 开始执行 | goal={goal['id']}")

        # 1. 规划
        try:
            context = self.plan(goal)
        except Exception as e:
            msg = f"规划阶段失败: {e}"
            log.error(f"[{self.name}] {msg}")
            return msg

        # 2. 执行
        try:
            result = self.execute(context)
        except Exception as e:
            msg = f"执行阶段失败: {e}"
            log.error(f"[{self.name}] {msg}")
            return msg

        # 3. 验证 + 4. 修复（最多 MAX_RETRIES 次）
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

        # 5. 汇报
        summary = self.report(result)
        log.info(f"[{self.name}] 完成 | {summary}")
        return summary

    # ── 子类必须实现 ─────────────────────────────────────

    @abstractmethod
    def plan(self, goal: dict) -> dict:
        """根据 goal 收集执行所需上下文，返回 context dict。"""

    @abstractmethod
    def execute(self, context: dict) -> dict:
        """执行核心业务逻辑，返回 result dict。"""

    @abstractmethod
    def verify(self, result: dict) -> tuple[bool, str]:
        """验证结果是否符合预期，返回 (是否通过, 问题描述)。"""

    @abstractmethod
    def fix(self, result: dict, issues: str) -> dict:
        """根据验证问题修复结果，返回修复后的 result dict。"""

    @abstractmethod
    def report(self, result: dict) -> str:
        """生成结果摘要字符串，供通知和日志使用。"""
