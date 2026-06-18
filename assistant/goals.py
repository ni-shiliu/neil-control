"""
目标管理。持久化到 goals.json，支持增删改查和状态切换。
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

GOALS_FILE = Path(__file__).parent / "goals.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load() -> list[dict]:
    if not GOALS_FILE.exists():
        return []
    return json.loads(GOALS_FILE.read_text(encoding="utf-8"))


def save(goals: list[dict]) -> None:
    GOALS_FILE.write_text(
        json.dumps(goals, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add(raw: str, schedule: str, loop: str) -> dict:
    goals = load()
    goal = {
        "id": f"goal_{uuid.uuid4().hex[:6]}",
        "raw": raw,
        "schedule": schedule,
        "loop": loop,
        "status": "active",
        "created_at": _now(),
        "last_run": None,
        "last_result": None,
    }
    goals.append(goal)
    save(goals)
    return goal


def get(goal_id: str) -> dict | None:
    return next((g for g in load() if g["id"] == goal_id), None)


def list_all() -> list[dict]:
    return load()


def delete(goal_id: str) -> bool:
    goals = load()
    new = [g for g in goals if g["id"] != goal_id]
    if len(new) == len(goals):
        return False
    save(new)
    return True


def _set_status(goal_id: str, status: str) -> bool:
    goals = load()
    for g in goals:
        if g["id"] == goal_id:
            g["status"] = status
            save(goals)
            return True
    return False


def pause(goal_id: str) -> bool:
    return _set_status(goal_id, "paused")


def resume(goal_id: str) -> bool:
    return _set_status(goal_id, "active")


def update_last_run(goal_id: str, result: str) -> None:
    goals = load()
    for g in goals:
        if g["id"] == goal_id:
            g["last_run"] = _now()
            g["last_result"] = result
            save(goals)
            return
