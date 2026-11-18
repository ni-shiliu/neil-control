"""Effect 模型与收集器。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha1
from pathlib import Path
from typing import Any


@dataclass
class Effect:
    effect_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    idempotency_key: str | None = None

    def resolved_idempotency_key(self) -> str:
        if self.idempotency_key:
            return self.idempotency_key
        raw = json.dumps(
            {"type": self.effect_type, "payload": self.payload},
            ensure_ascii=False,
            sort_keys=True,
        )
        return sha1(raw.encode("utf-8")).hexdigest()


class EffectCollector:
    """Loop 用来声明副作用意图，Engine 统一提交。"""

    def __init__(self):
        self._effects: list[Effect] = []

    def add(
        self,
        effect_type: str,
        payload: dict[str, Any],
        meta: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> None:
        self._effects.append(
            Effect(
                effect_type=effect_type,
                payload=payload,
                meta=meta or {},
                idempotency_key=idempotency_key,
            )
        )

    def drain(self) -> list[Effect]:
        effects = self._effects[:]
        self._effects.clear()
        return effects

    def __len__(self) -> int:
        return len(self._effects)


class EffectHistoryStore:
    """记录已成功提交的 effect，用于幂等去重。"""

    def __init__(self, path: Path | None = None):
        self.path = path or Path(__file__).parent.parent / "effect_history.json"

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def seen(self, key: str) -> bool:
        return key in self._load()

    def mark_seen(self, key: str, data: dict[str, Any]) -> None:
        current = self._load()
        current[key] = data
        self.path.write_text(
            json.dumps(current, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
