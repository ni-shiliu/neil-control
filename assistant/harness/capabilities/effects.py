"""⑥ 层 Effect outbox：稳定幂等键是重试和恢复边界。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal
import json
import os
import tempfile


EffectStatus = Literal["pending", "succeeded", "failed"]


@dataclass(frozen=True)
class EffectRecord:
    idempotency_key: str
    action_id: str
    status: EffectStatus
    result: str = ""


class EffectOutbox:
    def get(self, idempotency_key: str) -> EffectRecord | None:
        raise NotImplementedError

    def save(self, record: EffectRecord) -> None:
        raise NotImplementedError


class InMemoryEffectOutbox(EffectOutbox):
    def __init__(self):
        self._records: dict[str, EffectRecord] = {}

    def get(self, idempotency_key: str) -> EffectRecord | None:
        return self._records.get(idempotency_key)

    def save(self, record: EffectRecord) -> None:
        self._records[record.idempotency_key] = record


class JsonEffectOutbox(EffectOutbox):
    def __init__(self, base_dir: Path):
        self._base_dir = base_dir

    def get(self, idempotency_key: str) -> EffectRecord | None:
        path = self._path(idempotency_key)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return EffectRecord(**value)

    def save(self, record: EffectRecord) -> None:
        path = self._path(record.idempotency_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(prefix=".effect-", suffix=".json", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(asdict(record), handle, ensure_ascii=False, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)

    def _path(self, idempotency_key: str) -> Path:
        return self._base_dir / f"{idempotency_key}.json"
