"""用户偏好（preferences）的清洗与展开工具。

从 main.py 抽出，作为 engine 与 cli 都可依赖的中立模块，
解开原先 engine/chat_tools.py `from main import ...` 的反向依赖。
"""

from __future__ import annotations

_PREFERENCE_BUCKETS = {"content", "delivery", "behavior", "format"}


def flatten_preference_keys(data: dict, prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in data.items():
        current = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            keys.extend(flatten_preference_keys(value, current))
        else:
            keys.append(current)
    return keys


def sanitize_preferences(preferences: dict | None) -> dict | None:
    if not isinstance(preferences, dict):
        return None

    sanitized: dict = {}
    for key, value in preferences.items():
        if key not in _PREFERENCE_BUCKETS:
            continue
        if isinstance(value, dict):
            sanitized[key] = value
    return sanitized or None
