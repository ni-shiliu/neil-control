"""
MemoryStore — 持久化 Loop 的跨 run 记忆。

每个 Loop 对应 memory/{loop_name}.json，结构由 Loop 自己定义。
Engine 负责在每次执行前 load、执行后 save。
"""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).parent.parent / "memory"


class MemoryStore:

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or _BASE_DIR
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, loop_name: str) -> Path:
        return self.base_dir / f"{loop_name}.json"

    def load(self, loop_name: str) -> dict:
        path = self._path(loop_name)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning(f"[memory] 加载 {loop_name} 失败，重置为空: {e}")
            return {}

    def save(self, loop_name: str, data: dict) -> None:
        path = self._path(loop_name)
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            log.error(f"[memory] 保存 {loop_name} 失败: {e}")

    def merge_save(self, loop_name: str, updates: dict) -> dict:
        """把 updates merge 进现有记忆并保存，返回合并后的完整记忆。"""
        current = self.load(loop_name)
        merged = {**current, **updates}
        self.save(loop_name, merged)
        return merged

    def append_list(self, loop_name: str, key: str, item: dict, max_size: int = 100) -> None:
        """向列表字段追加一条记录，保留最近 max_size 条。"""
        current = self.load(loop_name)
        lst = current.get(key, [])
        lst.append(item)
        current[key] = lst[-max_size:]
        self.save(loop_name, current)
