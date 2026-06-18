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

def notify_telegram(message: str, parse_mode: str = "HTML") -> None:
    """发送 Telegram 文本消息。"""
    try:
        url = TELEGRAM_API.format(token=_tg_token(), method="sendMessage")
        resp = requests.post(url, json={
            "chat_id": _tg_chat_id(),
            "text": message,
            "parse_mode": parse_mode,
        }, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Telegram 文本发送失败: {e}")
        raise


def notify_telegram_document(file_path: str, caption: str = "") -> None:
    """发送 HTML 文件到 Telegram。"""
    try:
        url = TELEGRAM_API.format(token=_tg_token(), method="sendDocument")
        with open(file_path, "rb") as f:
            resp = requests.post(url, data={
                "chat_id": _tg_chat_id(),
                "caption": caption,
            }, files={"document": f}, timeout=120)
        resp.raise_for_status()
    except Exception as e:
        log.error(f"Telegram 文件发送失败: {e}")
        raise


# ── 预留 ─────────────────────────────────────────────────

def notify_wechat(title: str, message: str) -> None:
    raise NotImplementedError("微信通知尚未实现")
