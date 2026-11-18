"""Conversation 的按日期 JSONL 存储；thread 是记录字段，不是目录层级。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from harness.memory.conversation.models import ConversationRecord

try:  # pragma: no cover
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None


MAX_SEGMENT_BYTES = 8 * 1024 * 1024


def _segment(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in "-_." else "_" for char in value.strip())
    if not normalized or normalized in {".", ".."}:
        raise ValueError("Conversation 存储标识不能为空或路径非法")
    return normalized


class ConversationRepository:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path(__file__).parents[1] / "memory_store" / "conversations"

    def _user_dir(self, record: ConversationRecord) -> Path:
        return self.base_dir / "tenants" / _segment(record.tenant_id) / "users" / _segment(record.user_id)

    @staticmethod
    def _records_in(path: Path) -> list[ConversationRecord]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        records: list[ConversationRecord] = []
        for line in lines:
            try:
                value = json.loads(line)
                value["tool_calls"] = tuple(value.get("tool_calls", ()))
                value["decision_trace"] = tuple(value.get("decision_trace", ()))
                records.append(ConversationRecord(**value))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        return records

    @staticmethod
    def _append(path: Path, record: ConversationRecord) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(record.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def save(self, record: ConversationRecord) -> Path:
        directory = self._user_dir(record)
        directory.mkdir(parents=True, exist_ok=True)
        day = record.created_at[:10]
        lock_path = directory / ".append.lock"
        with lock_path.open("a", encoding="utf-8") as lock:
            if fcntl is not None:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                paths = sorted(directory.glob(f"{day}_*.jsonl"))
                path = paths[-1] if paths else directory / f"{day}_001.jsonl"
                size = len(json.dumps(record.to_dict(), ensure_ascii=False).encode("utf-8")) + 1
                if path.exists() and path.stat().st_size + size > MAX_SEGMENT_BYTES:
                    path = directory / f"{day}_{len(paths) + 1:03d}.jsonl"
                if any(item.id == record.id for item_path in paths for item in self._records_in(item_path)):
                    raise ValueError(f"ConversationRecord 已存在: {record.id}")
                self._append(path, record)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return path

    def list_recent(self, *, tenant_id: str, user_id: str, thread_id: str, limit: int = 8) -> list[ConversationRecord]:
        directory = self.base_dir / "tenants" / _segment(tenant_id) / "users" / _segment(user_id)
        if not directory.exists():
            return []
        records = [
            record for path in directory.glob("*.jsonl") for record in self._records_in(path)
            if record.thread_id == thread_id
        ]
        return sorted(records, key=lambda item: item.created_at)[-limit:]

    def cleanup(self, *, referenced_source_refs: Iterable[str], retention_days: int = 30) -> int:
        pinned = set(referenced_source_refs)
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=retention_days)
        removed = 0
        for path in self.base_dir.glob("tenants/*/users/*/*.jsonl"):
            retained: list[ConversationRecord] = []
            for record in self._records_in(path):
                try:
                    created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00")).date()
                except ValueError:
                    retained.append(record)
                    continue
                if created < cutoff and record.source_ref not in pinned:
                    removed += 1
                else:
                    retained.append(record)
            if not retained:
                path.unlink(missing_ok=True)
            else:
                payload = "".join(json.dumps(item.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n" for item in retained)
                temporary = path.with_suffix(path.suffix + ".tmp")
                temporary.write_text(payload, encoding="utf-8")
                os.replace(temporary, path)
        return removed
