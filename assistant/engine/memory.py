"""
MemoryStore — 持久化 Loop 的跨 run 记忆。

当前支持两层：
- loop memory：跨 goal 的长期经验
- goal memory：某个 goal 专属的运行状态

兼容旧接口：
- load/save 仍默认操作 loop memory
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent / "memory"
GOAL_MEMORY_MAX_BYTES = 8 * 1024
LOOP_MEMORY_MAX_BYTES = 16 * 1024
DEFAULT_RECENT_LIMIT = 5
DEFAULT_PATTERN_LIMIT = 50
DEFAULT_TEXT_LIMIT = 240


class MemoryStore:

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or _BASE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.loop_dir = self.base_dir / "loops"
        self.goal_dir = self.base_dir / "goals"
        self.loop_dir.mkdir(parents=True, exist_ok=True)
        self.goal_dir.mkdir(parents=True, exist_ok=True)

    def _loop_path(self, loop_name: str) -> Path:
        return self.loop_dir / f"{loop_name}.json"

    def _goal_path(self, goal_id: str) -> Path:
        return self.goal_dir / f"{goal_id}.json"

    @staticmethod
    def _json_size(data: dict) -> int:
        return len(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    @staticmethod
    def _trim_text(value, limit: int = DEFAULT_TEXT_LIMIT):
        if not isinstance(value, str):
            return value
        compact = " ".join(value.split())
        if len(compact) <= limit:
            return compact
        return compact[: max(0, limit - 3)] + "..."

    @classmethod
    def _trim_value(cls, value):
        if isinstance(value, str):
            return cls._trim_text(value)
        if isinstance(value, list):
            return [cls._trim_value(item) for item in value]
        if isinstance(value, dict):
            return {k: cls._trim_value(v) for k, v in value.items()}
        return value

    @classmethod
    def _compact_list_field(cls, key: str, values: list, *, limit: int) -> list:
        trimmed = [cls._trim_value(item) for item in values if item not in (None, "", [], {})]
        if key in {"recent_english_phrases"}:
            deduped = []
            seen = set()
            for item in trimmed:
                if not isinstance(item, str):
                    continue
                normalized = item.casefold().strip()
                if not normalized or normalized in seen:
                    continue
                seen.add(normalized)
                deduped.append(item)
            return deduped[-limit:]
        if key in {"skip_patterns"}:
            deduped = []
            seen = set()
            for item in trimmed:
                if not isinstance(item, dict):
                    continue
                sender = str(item.get("sender", "")).strip().lower()
                if not sender or sender in seen:
                    continue
                seen.add(sender)
                deduped.append(
                    {
                        "sender": cls._trim_text(item.get("sender", ""), 120),
                        "reason": cls._trim_text(item.get("reason", ""), 120),
                    }
                )
            return deduped[-limit:]
        return trimmed[-limit:]

    @classmethod
    def _compact_common(cls, data: dict, *, recent_limit: int, pattern_limit: int) -> dict:
        compacted = {}
        for key, value in data.items():
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                limit = recent_limit
                if key == "skip_patterns":
                    limit = pattern_limit
                compacted[key] = cls._compact_list_field(key, value, limit=limit)
                if compacted[key] == []:
                    compacted.pop(key, None)
                continue
            compacted[key] = cls._trim_value(value)
        return compacted

    @classmethod
    def _compact_goal_memory(cls, data: dict) -> dict:
        compacted = cls._compact_common(
            data,
            recent_limit=DEFAULT_RECENT_LIMIT,
            pattern_limit=DEFAULT_PATTERN_LIMIT,
        )
        if cls._json_size(compacted) <= GOAL_MEMORY_MAX_BYTES:
            return compacted

        for key in ("recent_activity", "recent_briefings", "recent_runs", "recent_failures"):
            if isinstance(compacted.get(key), list):
                compacted[key] = compacted[key][-3:]
        if isinstance(compacted.get("recent_english_phrases"), list):
            compacted["recent_english_phrases"] = compacted["recent_english_phrases"][-3:]
        if isinstance(compacted.get("skip_patterns"), list):
            compacted["skip_patterns"] = compacted["skip_patterns"][-20:]
        compacted = {k: v for k, v in compacted.items() if v not in (None, "", [], {})}
        if cls._json_size(compacted) <= GOAL_MEMORY_MAX_BYTES:
            return compacted

        minimal_keys = (
            "last_run_id",
            "last_status",
            "last_summary",
            "last_result_keys",
            "last_updated_at",
            "last_today",
            "last_output_name",
            "last_delivery_count",
            "last_english_phrase",
            "unread_count",
            "last_counts",
            "last_subjects",
            "skip_patterns",
        )
        minimal = {
            key: compacted[key]
            for key in minimal_keys
            if key in compacted and compacted[key] not in (None, "", [], {})
        }
        if isinstance(minimal.get("skip_patterns"), list):
            minimal["skip_patterns"] = minimal["skip_patterns"][-10:]
        return minimal

    @classmethod
    def _compact_loop_memory(cls, data: dict) -> dict:
        compacted = cls._compact_common(
            data,
            recent_limit=DEFAULT_RECENT_LIMIT,
            pattern_limit=DEFAULT_PATTERN_LIMIT,
        )
        if cls._json_size(compacted) <= LOOP_MEMORY_MAX_BYTES:
            return compacted

        if isinstance(compacted.get("skip_patterns"), list):
            compacted["skip_patterns"] = compacted["skip_patterns"][-20:]
        for key in ("recent_activity", "recent_briefings"):
            if isinstance(compacted.get(key), list):
                compacted[key] = compacted[key][-3:]
        compacted = {k: v for k, v in compacted.items() if v not in (None, "", [], {})}
        if cls._json_size(compacted) <= LOOP_MEMORY_MAX_BYTES:
            return compacted

        minimal_keys = ("totals", "last_updated_at", "last_summary", "skip_patterns")
        minimal = {
            key: compacted[key]
            for key in minimal_keys
            if key in compacted and compacted[key] not in (None, "", [], {})
        }
        if isinstance(minimal.get("skip_patterns"), list):
            minimal["skip_patterns"] = minimal["skip_patterns"][-10:]
        return minimal

    @staticmethod
    def _load_path(path: Path, label: str) -> dict:
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"[memory] 加载 {label} 失败，重置为空: {e}")
            return {}

    @staticmethod
    def _save_path(path: Path, label: str, data: dict) -> None:
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log.error(f"[memory] 保存 {label} 失败: {e}")

    def load(self, loop_name: str) -> dict:
        return self.load_loop_memory(loop_name)

    def save(self, loop_name: str, data: dict) -> None:
        self.save_loop_memory(loop_name, data)

    def load_loop_memory(self, loop_name: str) -> dict:
        return self._load_path(self._loop_path(loop_name), f"loop:{loop_name}")

    def save_loop_memory(self, loop_name: str, data: dict) -> None:
        compacted = self._compact_loop_memory(data)
        before_size = self._json_size(data) if isinstance(data, dict) else 0
        after_size = self._json_size(compacted)
        if after_size < before_size:
            log.info(f"[memory] loop compacted loop={loop_name} bytes={before_size}->{after_size}")
        self._save_path(self._loop_path(loop_name), f"loop:{loop_name}", compacted)

    def load_goal_memory(self, goal_id: str) -> dict:
        return self._load_path(self._goal_path(goal_id), f"goal:{goal_id}")

    def save_goal_memory(self, goal_id: str, data: dict) -> None:
        compacted = self._compact_goal_memory(data)
        before_size = self._json_size(data) if isinstance(data, dict) else 0
        after_size = self._json_size(compacted)
        if after_size < before_size:
            log.info(f"[memory] goal compacted goal={goal_id} bytes={before_size}->{after_size}")
        self._save_path(self._goal_path(goal_id), f"goal:{goal_id}", compacted)

    def merge_save(self, loop_name: str, updates: dict) -> dict:
        """把 updates merge 进现有记忆并保存，返回合并后的完整记忆。"""
        current = self.load_loop_memory(loop_name)
        merged = {**current, **updates}
        self.save_loop_memory(loop_name, merged)
        return merged

    def append_list(self, loop_name: str, key: str, item: dict, max_size: int = 100) -> None:
        """向列表字段追加一条记录，保留最近 max_size 条。"""
        current = self.load_loop_memory(loop_name)
        lst = current.get(key, [])
        lst.append(item)
        current[key] = lst[-max_size:]
        self.save_loop_memory(loop_name, current)
