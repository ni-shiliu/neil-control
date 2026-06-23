"""
RunContext — 工具注入容器。

每次 LoopEngine.run() 时构建，传入 Loop 的 plan/execute/fix 方法。
Loop 通过 ctx.tools.imap / ctx.tools.claude 操作，不直接 import 工具库。
"""

from __future__ import annotations
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING
from engine.effects import EffectCollector

if TYPE_CHECKING:
    from engine.tools.claude_tool import ClaudeTool
    from engine.tools.imap_tool import IMAPTool
    from engine.tools.smtp_tool import SMTPTool
    from engine.tools.telegram_tool import TelegramTool


log = logging.getLogger(__name__)


@dataclass
class ToolRegistry:
    claude: "ClaudeTool | None" = None
    imap: "IMAPTool | None" = None
    smtp: "SMTPTool | None" = None
    telegram: "TelegramTool | None" = None

    @classmethod
    def build(cls, required: list[str]) -> "ToolRegistry":
        """按 Loop 声明的 required_tools 按需实例化工具。"""
        from engine.tools.claude_tool import ClaudeTool
        from engine.tools.imap_tool import IMAPTool
        from engine.tools.smtp_tool import SMTPTool
        from engine.tools.telegram_tool import TelegramTool

        def _safe_build(tool_name: str, factory):
            try:
                return factory()
            except Exception as e:
                log.warning(f"[tools] 初始化失败 tool={tool_name}: {e}")
                return None

        return cls(
            claude=_safe_build("claude", ClaudeTool) if "claude" in required else None,
            imap=_safe_build("imap", IMAPTool) if "imap" in required else None,
            smtp=_safe_build("smtp", SMTPTool) if "smtp" in required else None,
            telegram=_safe_build("telegram", TelegramTool) if "telegram" in required else None,
        )


@dataclass
class RunContext:
    goal: dict
    run_id: str = ""
    memory: dict = field(default_factory=dict)
    goal_memory: dict = field(default_factory=dict)
    recent_runs: dict = field(default_factory=dict)
    runtime_doc: str = ""
    loop_doc: str = ""
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    effects: EffectCollector = field(default_factory=EffectCollector)
