"""按租户、用户隔离的个人配置 JSON Repository。"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _segment(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in "-_." else "_" for char in value.strip())
    if not normalized or normalized in {".", ".."}:
        raise ValueError("个人配置标识非法")
    return normalized


class PersonalConfigRepository:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path(__file__).with_name("personal_store")

    def _path(self, tenant_id: str, user_id: str) -> Path:
        return self.base_dir / "tenants" / _segment(tenant_id) / "users" / f"{_segment(user_id)}.json"

    def load(self, *, tenant_id: str, user_id: str) -> dict:
        path = self._path(tenant_id, user_id)
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, *, tenant_id: str, user_id: str, config: dict) -> None:
        path = self._path(tenant_id, user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
