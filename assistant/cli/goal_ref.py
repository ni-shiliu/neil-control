"""goal 引用消解：把用户的模糊指代（别名/描述/近期/今天/指示代词）
解析并排序到具体的 goal_id。

从 main.py 原样抽出。依赖 goals 模块读取全部 goal。
"""

from __future__ import annotations

from datetime import datetime

import goals as goals_mod


def normalize_lookup_text(text: str) -> str:
    return "".join(ch.lower() for ch in text.strip() if not ch.isspace())


def goal_aliases(goal: dict) -> list[str]:
    loop_name = goal.get("loop", "")
    aliases = [goal.get("id", ""), goal.get("raw", ""), loop_name]
    if loop_name == "daily_briefing_loop":
        aliases.extend(["简报", "每日简报", "早报", "briefing"])
    if loop_name == "email_loop":
        aliases.extend(["邮件", "邮箱", "未读邮件", "邮件处理", "email", "mail"])
    return [alias for alias in aliases if alias]


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def goal_matches_today(goal: dict) -> bool:
    today = datetime.now().date().isoformat()
    last_run = goal.get("last_run") or ""
    created_at = goal.get("created_at") or ""
    return last_run.startswith(today) or created_at.startswith(today)


def goal_sort_timestamp(goal: dict) -> float:
    dt = parse_iso_datetime(goal.get("last_run")) or parse_iso_datetime(goal.get("created_at"))
    return dt.timestamp() if dt else 0.0


def resolve_goal_reference(goal_ref: str) -> list[dict]:
    normalized_ref = normalize_lookup_text(goal_ref)
    if not normalized_ref:
        return []

    candidates: list[dict] = []
    for goal in goals_mod.list_all():
        aliases = [normalize_lookup_text(alias) for alias in goal_aliases(goal)]
        if any(normalized_ref in alias or alias in normalized_ref for alias in aliases if alias):
            candidates.append(goal)
    return candidates


def rank_goal_candidates(candidates: list[dict], *, prefer_recent: bool = False, prefer_today: bool = False) -> list[dict]:
    def score(goal: dict) -> tuple[int, int, int, float]:
        recent_score = 1 if prefer_recent and (goal.get("last_run") or goal.get("created_at")) else 0
        today_score = 1 if prefer_today and goal_matches_today(goal) else 0
        active_score = 1 if goal.get("status") == "active" else 0
        return (today_score, recent_score, active_score, goal_sort_timestamp(goal))

    return sorted(candidates, key=score, reverse=True)


def select_goal_from_ref(goal_ref: str, *, prefer_recent: bool = False, prefer_today: bool = False, deictic: bool = False) -> str | None:
    candidates = resolve_goal_reference(goal_ref)
    if not candidates:
        return None

    ranked = rank_goal_candidates(
        candidates,
        prefer_recent=prefer_recent or deictic,
        prefer_today=prefer_today,
    )
    if len(ranked) > 1:
        top = ranked[0]
        second = ranked[1]
        if goal_sort_timestamp(top) != goal_sort_timestamp(second) or top.get("status") != second.get("status"):
            return top["id"]
        return None
    return ranked[0]["id"]
