"""Conversation records persisted by day with rolling parts."""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

RETENTION_DAYS = 3
MAX_FILE_BYTES = 128 * 1024
MAX_RECORDS_PER_FILE = 100
MAX_TEXT_PREVIEW = 800


@dataclass
class ConversationRecord:
    turn_id: str
    ts: str
    user_input: str
    assistant_response: str
    route: str
    command: str | None = None
    ai_result: dict[str, Any] | None = None
    execution: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConversationRecorder:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path(__file__).parent / "conversation_records"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _today_utc() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    @staticmethod
    def _retention_cutoff_date() -> str:
        return (datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS - 1)).isoformat()

    @staticmethod
    def _record_date(record: ConversationRecord) -> str:
        return record.ts[:10]

    @staticmethod
    def _record_part(path: Path) -> int:
        stem = path.stem
        try:
            return int(stem.rsplit("_", 1)[-1])
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _path_record_date(path: Path) -> str | None:
        parts = path.stem.split("_")
        if len(parts) < 4:
            return None
        maybe_date = parts[-2]
        try:
            datetime.strptime(maybe_date, "%Y-%m-%d")
            return maybe_date
        except ValueError:
            return None

    def _daily_paths(self, date_str: str) -> list[Path]:
        paths = list(self.base_dir.glob(f"conversation_records_{date_str}_*.json"))
        return sorted(paths, key=self._record_part)

    def _next_path(self, date_str: str) -> Path:
        paths = self._daily_paths(date_str)
        if not paths:
            return self.base_dir / f"conversation_records_{date_str}_001.json"
        next_part = self._record_part(paths[-1]) + 1
        return self.base_dir / f"conversation_records_{date_str}_{next_part:03d}.json"

    @staticmethod
    def _payload_size(payload: dict[str, Any]) -> int:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _compact_text(value: str, limit: int = MAX_TEXT_PREVIEW) -> str:
        compact = " ".join(value.split())
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 3)] + "..."

    @classmethod
    def _compact_record(cls, record: dict[str, Any]) -> dict[str, Any]:
        compacted = dict(record)
        compacted["user_input"] = cls._compact_text(compacted.get("user_input", ""), 400)
        compacted["assistant_response"] = cls._compact_text(compacted.get("assistant_response", ""), 800)
        ai_result = compacted.get("ai_result")
        if isinstance(ai_result, dict):
            compacted["ai_result"] = ai_result
        return compacted

    def cleanup_old_files(self) -> int:
        deleted = 0
        cutoff = self._retention_cutoff_date()
        for path in self.base_dir.glob("conversation_records_*.json"):
            record_date = self._path_record_date(path)
            if not record_date or record_date >= cutoff:
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError as e:
                log.error(f"[conversation_records] 删除过期文件失败 path={path.name}: {e}")
        if deleted:
            log.info(f"[conversation_records] 已删除 {deleted} 个超过 {RETENTION_DAYS} 天的历史文件")
        return deleted

    def _load_payload(self, path: Path, *, date_str: str) -> dict[str, Any]:
        if not path.exists():
            return {"date": date_str, "records": []}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {"date": date_str, "records": []}

    def save(self, record: ConversationRecord) -> Path:
        date_str = self._record_date(record)
        paths = self._daily_paths(date_str)
        path = paths[-1] if paths else self.base_dir / f"conversation_records_{date_str}_001.json"
        payload = self._load_payload(path, date_str=date_str)
        records = payload.get("records", [])
        compacted = self._compact_record(record.to_dict())

        candidate_payload = {
            "date": date_str,
            "records": [*records, compacted],
        }
        if records and (
            len(records) >= MAX_RECORDS_PER_FILE
            or self._payload_size(candidate_payload) > MAX_FILE_BYTES
        ):
            path = self._next_path(date_str)
            payload = {"date": date_str, "records": []}
            records = []

        records = [r for r in records if r.get("turn_id") != record.turn_id]
        records.append(compacted)
        payload["date"] = date_str
        payload["records"] = records
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def list_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        paths = sorted(
            self.base_dir.glob("conversation_records_*.json"),
            key=lambda item: (
                self._path_record_date(item) or "",
                self._record_part(item),
            ),
            reverse=True,
        )
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            daily_records = payload.get("records", [])
            if isinstance(daily_records, list):
                records.extend(reversed(daily_records))
            if len(records) >= limit:
                break
        records.sort(key=lambda item: item.get("ts", ""), reverse=True)
        return records[:limit]

    def build_record(
        self,
        *,
        user_input: str,
        assistant_response: str,
        route: str,
        command: str | None = None,
        ai_result: dict[str, Any] | None = None,
        execution: dict[str, Any] | None = None,
    ) -> ConversationRecord:
        return ConversationRecord(
            turn_id=uuid.uuid4().hex[:12],
            ts=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            user_input=user_input,
            assistant_response=assistant_response,
            route=route,
            command=command,
            ai_result=ai_result,
            execution=execution or {},
        )
