"""TelegramTool — 封装 Telegram Bot 通知。"""

import logging
from notifier import TelegramBot

log = logging.getLogger(__name__)


class TelegramTool:

    def __init__(self):
        self._bot = TelegramBot()

    def send(self, message: str, parse_mode: str = "HTML") -> None:
        try:
            self._bot.send_message(message, parse_mode=parse_mode)
        except Exception as e:
            log.error(f"[telegram] 发送失败: {e}")
            raise

    def send_document(self, file_path: str, caption: str = "") -> None:
        try:
            self._bot.send_document(file_path, caption=caption)
        except Exception as e:
            log.error(f"[telegram] 文件发送失败: {e}")
            raise
