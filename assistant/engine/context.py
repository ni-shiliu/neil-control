"""
RunContext — 工具注入容器。

每次 LoopEngine.run() 时构建，传入 Loop 的 plan/execute/fix 方法。
Loop 通过 ctx.tools.imap / ctx.tools.claude 操作，不直接 import 工具库。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.tools.claude_tool import ClaudeTool
    from engine.tools.imap_tool import IMAPTool
    from engine.tools.smtp_tool import SMTPTool
    from engine.tools.telegram_tool import TelegramTool


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

        return cls(
            claude=ClaudeTool() if "claude" in required else None,
            imap=IMAPTool() if "imap" in required else None,
            smtp=SMTPTool() if "smtp" in required else None,
            telegram=TelegramTool() if "telegram" in required else None,
        )


@dataclass
class RunContext:
    goal: dict
    memory: dict = field(default_factory=dict)
    tools: ToolRegistry = field(default_factory=ToolRegistry)
