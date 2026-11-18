"""RunRecord — 持久化每次 Loop 运行的结构化记录。"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)
MAX_TEXT_PREVIEW = 500
MAX_DAILY_RECORD_BYTES = 256 * 1024
KEEP_DETAILED_RUNS = 3
RETENTION_DAYS = 3


@dataclass
class RunRecord:
    run_id: str
    goal_id: str
    loop_name: str
    status: str
    trigger_mode: str | None
    started_at: str
    ended_at: str
    duration_ms: int
    summary: str
    result: dict[str, Any] = field(default_factory=dict)
    phase_data: dict[str, Any] = field(default_factory=dict)
    planned_effects: list[dict[str, Any]] = field(default_factory=list)
    committed_effects: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    memory_before: dict[str, Any] = field(default_factory=dict)
    memory_after: dict[str, Any] = field(default_factory=dict)
    goal_memory_before: dict[str, Any] = field(default_factory=dict)
    goal_memory_after: dict[str, Any] = field(default_factory=dict)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    next_trigger_in_seconds: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RunRecorder:
    """按天按 goal 落盘运行记录，便于排障和查看每日执行轨迹。"""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path(__file__).parent.parent / "run_records"
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _record_date(record: RunRecord) -> str:
        raw = record.started_at[:8]
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"

    def _path_for(self, record: RunRecord) -> Path:
        return self.base_dir / f"{record.goal_id}_{record.loop_name}_{self._record_date(record)}.json"

    @staticmethod
    def _retention_cutoff_date() -> str:
        return (datetime.now(timezone.utc).date() - timedelta(days=RETENTION_DAYS - 1)).isoformat()

    @staticmethod
    def _path_record_date(path: Path) -> str | None:
        stem = path.stem
        if len(stem) < 10:
            return None
        maybe_date = stem[-10:]
        try:
            datetime.strptime(maybe_date, "%Y-%m-%d")
            return maybe_date
        except ValueError:
            return None

    def cleanup_old_files(self) -> int:
        deleted = 0
        cutoff = self._retention_cutoff_date()
        for path in self.base_dir.glob("*.json"):
            record_date = self._path_record_date(path)
            if not record_date or record_date >= cutoff:
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError as e:
                log.error(f"[run_records] 删除过期文件失败 path={path.name}: {e}")
        if deleted:
            log.info(f"[run_records] 已删除 {deleted} 个超过 {RETENTION_DAYS} 天的历史文件")
        return deleted

    @staticmethod
    def _trim_text(value: Any, limit: int = MAX_TEXT_PREVIEW) -> Any:
        if not isinstance(value, str):
            return value
        compact = " ".join(value.split())
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 3)] + "..."

    @classmethod
    def _compact_output(cls, output: dict[str, Any]) -> dict[str, Any]:
        compacted = {
            "output_type": output.get("output_type", ""),
            "name": output.get("name", ""),
            "meta": output.get("meta", {}),
        }
        content = output.get("content")
        if isinstance(content, str):
            compacted["content_preview"] = cls._trim_text(content)
            compacted["content_length"] = len(content)
        return compacted

    @classmethod
    def _compact_result(cls, result: dict[str, Any]) -> dict[str, Any]:
        compacted: dict[str, Any] = {}
        for key, value in result.items():
            if key == "outputs" and isinstance(value, list):
                compacted["outputs"] = [
                    cls._compact_output(item)
                    for item in value
                    if isinstance(item, dict)
                ]
                continue
            if key in {"html", "content", "body", "reply"} and isinstance(value, str):
                compacted[f"{key}_preview"] = cls._trim_text(value)
                compacted[f"{key}_length"] = len(value)
                continue
            if isinstance(value, str):
                compacted[key] = cls._trim_text(value)
            else:
                compacted[key] = value
        return compacted

    @classmethod
    def _compact_record(cls, record: RunRecord) -> dict[str, Any]:
        payload = record.to_dict()
        payload["result"] = cls._compact_result(payload.get("result", {}))
        return payload

    @classmethod
    def _summary_only_record(cls, run: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": run.get("run_id", ""),
            "goal_id": run.get("goal_id", ""),
            "loop_name": run.get("loop_name", ""),
            "status": run.get("status", ""),
            "trigger_mode": run.get("trigger_mode"),
            "started_at": run.get("started_at", ""),
            "ended_at": run.get("ended_at", ""),
            "duration_ms": run.get("duration_ms", 0),
            "summary": cls._trim_text(run.get("summary", ""), 240),
            "dry_run": bool(run.get("dry_run", False)),
            "error": cls._trim_text(run.get("error"), 240) if run.get("error") else None,
            "compacted": True,
            "compaction_mode": "summary_only",
        }

    @staticmethod
    def _payload_size(payload: dict[str, Any]) -> int:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @classmethod
    def _enforce_daily_size_limit(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if cls._payload_size(payload) <= MAX_DAILY_RECORD_BYTES:
            return payload

        runs = list(payload.get("runs", []))
        if not runs:
            return payload

        trimmed_runs = []
        for idx, run in enumerate(runs):
            if idx < KEEP_DETAILED_RUNS:
                trimmed_runs.append(run)
            else:
                trimmed_runs.append(cls._summary_only_record(run))

        compacted_payload = {**payload, "runs": trimmed_runs}
        compacted_payload["compacted"] = True
        compacted_payload["compaction_mode"] = "mixed_recent_detail"
        compacted_payload["compaction_threshold_bytes"] = MAX_DAILY_RECORD_BYTES
        compacted_payload["detailed_runs_kept"] = KEEP_DETAILED_RUNS

        if cls._payload_size(compacted_payload) > MAX_DAILY_RECORD_BYTES:
            compacted_payload["runs"] = [cls._summary_only_record(run) for run in runs]
            compacted_payload["compaction_mode"] = "summary_only_all"

        return compacted_payload

    def save(self, record: RunRecord) -> Path:
        path = self._path_for(record)
        payload = {
            "goal_id": record.goal_id,
            "loop_name": record.loop_name,
            "date": self._record_date(record),
            "runs": [],
        }
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                payload = {
                    "goal_id": record.goal_id,
                    "loop_name": record.loop_name,
                    "date": self._record_date(record),
                    "runs": [],
                }

        runs = [r for r in payload.get("runs", []) if r.get("run_id") != record.run_id]
        runs.append(self._compact_record(record))
        runs.sort(key=lambda item: item.get("started_at", ""), reverse=True)
        payload["goal_id"] = record.goal_id
        payload["loop_name"] = record.loop_name
        payload["date"] = self._record_date(record)
        payload["runs"] = runs
        before_size = self._payload_size(payload)
        payload = self._enforce_daily_size_limit(payload)
        after_size = self._payload_size(payload)
        if after_size < before_size:
            log.warning(
                "[run_records] compacted daily file goal=%s loop=%s bytes=%s->%s mode=%s",
                record.goal_id,
                record.loop_name,
                before_size,
                after_size,
                payload.get("compaction_mode", "unknown"),
            )
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def list_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        paths = sorted(self.base_dir.glob("*.json"), reverse=True)
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                records.extend(payload.get("runs", []))
            except Exception:
                continue
        records.sort(key=lambda item: item.get("started_at", ""), reverse=True)
        return records[:limit]

    def find_run(self, run_id: str) -> dict[str, Any] | None:
        for path in sorted(self.base_dir.glob("*.json"), reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                for run in payload.get("runs", []):
                    if run.get("run_id") == run_id:
                        return run
            except Exception:
                continue
        return None

    @staticmethod
    def _project_recent(run: dict[str, Any]) -> dict[str, Any]:
        result = run.get("result", {})
        projected_result = {}
        if isinstance(result, dict):
            if "english_phrase" in result:
                projected_result["english_phrase"] = result.get("english_phrase", "")
            if "outputs" in result:
                projected_result["outputs"] = [
                    item.get("name", "")
                    for item in result.get("outputs", [])
                    if isinstance(item, dict) and item.get("name")
                ][:5]
            if "last_subjects" in result:
                projected_result["last_subjects"] = result.get("last_subjects", {})
            if "deliveries" in result:
                projected_result["delivery_count"] = len(result.get("deliveries", []))

        return {
            "run_id": run.get("run_id", ""),
            "goal_id": run.get("goal_id", ""),
            "loop_name": run.get("loop_name", ""),
            "status": run.get("status", ""),
            "summary": run.get("summary", ""),
            "started_at": run.get("started_at", ""),
            "error": run.get("error"),
            "result": projected_result,
        }

    def list_recent_by_goal(self, goal_id: str, limit: int = 5) -> list[dict[str, Any]]:
        records = []
        for run in self.list_recent(limit=500):
            if run.get("goal_id") == goal_id:
                records.append(self._project_recent(run))
            if len(records) >= limit:
                break
        return records

    def list_recent_by_loop(self, loop_name: str, limit: int = 5) -> list[dict[str, Any]]:
        records = []
        for run in self.list_recent(limit=500):
            if run.get("loop_name") == loop_name:
                records.append(self._project_recent(run))
            if len(records) >= limit:
                break
        return records
