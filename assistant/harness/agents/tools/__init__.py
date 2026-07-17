"""具体能力实现的惰性导出，避免未使用的供应商依赖污染 Harness 启动。"""

__all__ = ["ClaudeTool", "IMAPTool", "SMTPTool", "TelegramTool"]


def __getattr__(name: str):
    if name == "ClaudeTool":
        from harness.agents.tools.claude_tool import ClaudeTool
        return ClaudeTool
    if name == "IMAPTool":
        from harness.agents.tools.imap_tool import IMAPTool
        return IMAPTool
    if name == "SMTPTool":
        from harness.agents.tools.smtp_tool import SMTPTool
        return SMTPTool
    if name == "TelegramTool":
        from harness.agents.tools.telegram_tool import TelegramTool
        return TelegramTool
    raise AttributeError(name)
