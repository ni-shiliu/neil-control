"""
通知模块。

当前实现：
  - macOS 系统通知（osascript）
  - Telegram Bot（发文本 + 发 HTML 文件）

预留：notify_wechat()
"""

import logging
import os
import subprocess

import requests

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def _tg_token() -> str:
    return os.environ["TELEGRAM_BOT_TOKEN"]


def _tg_chat_id() -> str:
    return os.environ["TELEGRAM_CHAT_ID"]


# ── macOS 系统通知 ────────────────────────────────────────

def notify(title: str, message: str) -> None:
    try:
        script = f'display notification "{message}" with title "{title}"'
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except Exception as e:
        log.warning(f"系统通知发送失败: {e}")


# ── Telegram ─────────────────────────────────────────────

class TelegramBot:
    """Telegram Bot 工具类，统一管理 token/chat_id 和重试逻辑。"""

    def __init__(self, token: str | None = None, chat_id: str | None = None,
                 timeout: int = 30, retries: int = 3):
        self.token = token or _tg_token()
        self.chat_id = chat_id or _tg_chat_id()
        self.timeout = timeout
        self.retries = retries

    def _url(self, method: str) -> str:
        return TELEGRAM_API.format(token=self.token, method=method)

    def _post(self, method: str, **kwargs) -> requests.Response:
        last_exc = None
        for attempt in range(1, self.retries + 1):
            try:
                resp = requests.post(self._url(method), timeout=self.timeout, **kwargs)
                resp.raise_for_status()
                return resp
            except Exception as e:
                last_exc = e
                log.warning(f"Telegram {method} 第{attempt}次失败: {e}")
        raise last_exc

    def send_message(self, text: str, parse_mode: str = "HTML") -> None:
        self._post("sendMessage", json={
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
        })

    def send_document(self, file_path: str, caption: str = "") -> None:
        with open(file_path, "rb") as f:
            self._post("sendDocument", data={
                "chat_id": self.chat_id,
                "caption": caption,
            }, files={"document": f})


# 模块级默认实例，兼容旧调用
_bot: TelegramBot | None = None


def _get_bot() -> TelegramBot:
    global _bot
    if _bot is None:
        _bot = TelegramBot()
    return _bot


def notify_telegram(message: str, parse_mode: str = "HTML") -> None:
    try:
        _get_bot().send_message(message, parse_mode=parse_mode)
    except Exception as e:
        log.error(f"Telegram 文本发送失败: {e}")
        raise


def notify_telegram_document(file_path: str, caption: str = "") -> None:
    try:
        _get_bot().send_document(file_path, caption=caption)
    except Exception as e:
        log.error(f"Telegram 文件发送失败: {e}")
        raise


# ── 预留 ─────────────────────────────────────────────────

def notify_wechat(title: str, message: str) -> None:
    raise NotImplementedError("微信通知尚未实现")
