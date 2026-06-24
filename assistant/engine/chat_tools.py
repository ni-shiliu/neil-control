"""
chat_tools — CLI agentic loop 的工具集。

每个 tool 对应一类用户意图，Claude 通过 tool_use 驱动执行。
包含两部分：
  TOOL_SCHEMAS  : 传给 Claude API 的 tools 列表（JSON schema）
  execute_tool  : 根据 tool name + input 执行并返回字符串结果
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.chat import ChatHarness

log = logging.getLogger(__name__)

# 这些工具的结果直接打印给用户，不经过 Claude 转述（避免格式丢失或内容被压缩）
_PRINT_DIRECT = {"list_goals", "show_goal"}

# ── Tool Schemas ─────────────────────────────────────────────────────────────

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "list_goals",
        "description": "列出所有 goal，可按状态过滤。用于用户询问「有哪些目标」、「现在在跑什么」等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "enum": ["all", "active", "paused"],
                    "description": "过滤状态：all=全部，active=运行中，paused=已暂停",
                }
            },
            "required": [],
        },
    },
    {
        "name": "show_goal",
        "description": "查看单个 goal 的完整配置，包括调度、loop、最近运行结果等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "goal 的 ID，如 goal_df6f5c"},
            },
            "required": ["goal_id"],
        },
    },
    {
        "name": "pause_goal",
        "description": "暂停一个 goal，使其不再按计划运行。",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "要暂停的 goal ID"},
            },
            "required": ["goal_id"],
        },
    },
    {
        "name": "resume_goal",
        "description": "恢复一个已暂停的 goal，使其重新按计划运行。",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "要恢复的 goal ID"},
            },
            "required": ["goal_id"],
        },
    },
    {
        "name": "delete_goal",
        "description": "删除一个 goal。删除前会向用户确认。",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "要删除的 goal ID"},
            },
            "required": ["goal_id"],
        },
    },
    {
        "name": "rerun_goal",
        "description": "立即触发一次 goal 执行，不等待下次调度时间。",
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "要立即执行的 goal ID"},
                "dry_run": {
                    "type": "boolean",
                    "description": "是否以 dry_run 模式执行（不产生实际副作用）",
                },
            },
            "required": ["goal_id"],
        },
    },
    {
        "name": "create_goal",
        "description": (
            "创建一个新的自动化目标。需要明确调度时间或触发条件，以及对应的 loop 类型。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "raw": {"type": "string", "description": "用户原始描述，原文保留"},
                "loop": {"type": "string", "description": "loop 名称，如 daily_briefing_loop"},
                "trigger_mode": {
                    "type": "string",
                    "enum": ["cron", "goal", "event"],
                    "description": "触发模式",
                },
                "schedule": {
                    "type": "string",
                    "description": "5字段 cron 表达式，trigger_mode=event 时为 null",
                },
                "goal_condition": {
                    "type": "string",
                    "description": "goal 模式时的完成条件描述",
                },
                "summary": {"type": "string", "description": "一句话摘要"},
                "dry_run": {"type": "boolean", "description": "是否 dry_run，默认 false"},
                "retry_after_minutes": {"type": "integer"},
                "max_retries": {"type": "integer"},
                "retry_backoff_factor": {"type": "integer"},
                "retry_max_minutes": {"type": "integer"},
            },
            "required": ["raw", "loop", "trigger_mode", "summary"],
        },
    },
    {
        "name": "update_goal_preferences",
        "description": (
            "更新某个 goal 的个性化偏好，例如简报内容偏好、邮件处理规则等。"
            "偏好存入该 goal 的 memory，loop 执行时会读取。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "目标 goal ID"},
                "preferences": {
                    "type": "object",
                    "description": "偏好内容，key 只能是 content/delivery/behavior/format",
                    "properties": {
                        "content": {"type": "object"},
                        "delivery": {"type": "object"},
                        "behavior": {"type": "object"},
                        "format": {"type": "object"},
                    },
                },
            },
            "required": ["goal_id", "preferences"],
        },
    },
    {
        "name": "update_loop_preferences",
        "description": (
            "更新某个 loop 的全局默认偏好，影响该 loop 下所有 goal 的执行。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "loop": {"type": "string", "description": "loop 名称"},
                "preferences": {
                    "type": "object",
                    "description": "偏好内容，key 只能是 content/delivery/behavior/format",
                    "properties": {
                        "content": {"type": "object"},
                        "delivery": {"type": "object"},
                        "behavior": {"type": "object"},
                        "format": {"type": "object"},
                    },
                },
            },
            "required": ["loop", "preferences"],
        },
    },
    {
        "name": "update_user_preferences",
        "description": (
            "更新用户个人长期偏好，如语言、时区、通知风格等。"
            "也可以为 goal 设置别名（nickname），方便下次用自然语言引用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "preferences": {
                    "type": "object",
                    "description": "个人偏好，key 只能是 content/delivery/behavior/format",
                    "properties": {
                        "content": {"type": "object"},
                        "delivery": {"type": "object"},
                        "behavior": {"type": "object"},
                        "format": {"type": "object"},
                    },
                },
                "goal_nicknames": {
                    "type": "object",
                    "description": "goal 别名映射，如 {\"邮件\": \"goal_f83b90\"}",
                },
            },
            "required": [],
        },
    },
]

# ── Tool Executor ─────────────────────────────────────────────────────────────

def execute_tool(name: str, tool_input: dict, harness: "ChatHarness") -> str:
    """
    执行 tool，返回给 Claude 的字符串结果。
    _PRINT_DIRECT 中的查询工具直接打印给用户，其余由 Claude 转述。
    """
    try:
        _dispatch: dict = {
            "list_goals":               lambda: _tool_list_goals(tool_input),
            "show_goal":                lambda: _tool_show_goal(tool_input),
            "pause_goal":               lambda: _tool_pause_goal(tool_input),
            "resume_goal":              lambda: _tool_resume_goal(tool_input),
            "delete_goal":              lambda: _tool_delete_goal(tool_input),
            "rerun_goal":               lambda: _tool_rerun_goal(tool_input),
            "create_goal":              lambda: _tool_create_goal(tool_input),
            "update_goal_preferences":  lambda: _tool_update_goal_preferences(tool_input, harness),
            "update_loop_preferences":  lambda: _tool_update_loop_preferences(tool_input, harness),
            "update_user_preferences":  lambda: _tool_update_user_preferences(tool_input, harness),
        }
        if name not in _dispatch:
            return f"未知 tool: {name}"
        result = _dispatch[name]()
        if name in _PRINT_DIRECT:
            print(result)
        return result
    except Exception as e:
        log.error(f"[chat_tools] tool={name} 执行异常: {e}", exc_info=True)
        return f"执行失败: {e}"


# ── 各 tool 实现 ──────────────────────────────────────────────────────────────

def _tool_list_goals(inp: dict) -> str:
    import goals as goals_mod
    status_filter = inp.get("status_filter", "all")
    all_goals = goals_mod.list_all()
    if status_filter == "active":
        goals = [g for g in all_goals if g.get("status") == "active"]
    elif status_filter == "paused":
        goals = [g for g in all_goals if g.get("status") == "paused"]
    else:
        goals = all_goals

    if not goals:
        return f"当前没有{'运行中的' if status_filter == 'active' else '暂停的' if status_filter == 'paused' else ''} goal。"

    lines = [f"共 {len(goals)} 个 goal："]
    for idx, g in enumerate(goals, 1):
        status_icon = "✓" if g["status"] == "active" else "⏸"
        schedule_label = g.get("schedule") or "-"
        lines.append(
            f"{idx}. {g['id']} {status_icon} {g['status']} | "
            f"loop={g['loop']} | schedule={schedule_label} | {g['raw']}"
        )
        if g.get("last_run"):
            lines.append(f"   上次运行: {g['last_run']}  结果: {g.get('last_result', '')}")
    return "\n".join(lines)


def _tool_show_goal(inp: dict) -> str:
    import goals as goals_mod
    goal_id = inp.get("goal_id", "")
    goal = goals_mod.get(goal_id)
    if not goal:
        return f"未找到 goal: {goal_id}"
    lines = [
        f"Goal ID:   {goal.get('id', '')}",
        f"状态:      {goal.get('status', '')}",
        f"Loop:      {goal.get('loop', '')}",
        f"触发模式:  {goal.get('trigger_mode', 'cron')}",
        f"Schedule:  {goal.get('schedule', '')}",
        f"描述:      {goal.get('raw', '')}",
        f"创建时间:  {goal.get('created_at', '')}",
        f"最近运行:  {goal.get('last_run', '(未运行)')}",
        f"最近结果:  {goal.get('last_result', '')}",
        f"Dry Run:   {goal.get('dry_run', False)}",
    ]
    if goal.get("goal_condition"):
        lines.append(f"Goal条件:  {goal['goal_condition']}")
    retry_after = goal.get("retry_after_minutes")
    if retry_after is not None:
        lines.append(
            f"重试:      间隔 {retry_after}min, 最多 {goal.get('max_retries')} 次, "
            f"退避 {goal.get('retry_backoff_factor')}x, 上限 {goal.get('retry_max_minutes')}min"
        )
    return "\n".join(lines)


def _tool_pause_goal(inp: dict) -> str:
    import goals as goals_mod
    import scheduler
    goal_id = inp.get("goal_id", "")
    if goals_mod.pause(goal_id):
        scheduler.pause_goal(goal_id)
        return f"已暂停 {goal_id}"
    return f"未找到目标 {goal_id}"


def _tool_resume_goal(inp: dict) -> str:
    import goals as goals_mod
    import scheduler
    goal_id = inp.get("goal_id", "")
    if goals_mod.resume(goal_id):
        scheduler.resume_goal(goal_id)
        return f"已恢复 {goal_id}"
    return f"未找到目标 {goal_id}"


def _tool_delete_goal(inp: dict) -> str:
    import goals as goals_mod
    import scheduler
    goal_id = inp.get("goal_id", "")
    if not goals_mod.get(goal_id):
        return f"未找到目标 {goal_id}"
    confirm = input(f"确认删除 {goal_id}？(y/N) ").strip().lower()
    if confirm != "y":
        return "用户已取消删除。"
    if goals_mod.delete(goal_id):
        scheduler.remove_goal(goal_id)
        return f"已删除 {goal_id}"
    return f"删除失败 {goal_id}"


def _tool_rerun_goal(inp: dict) -> str:
    import goals as goals_mod
    import scheduler
    goal_id = inp.get("goal_id", "")
    goal = goals_mod.get(goal_id)
    if not goal:
        return f"未找到目标 {goal_id}"
    if goal.get("status") != "active":
        return f"goal {goal_id} 当前为 {goal.get('status')} 状态，不能执行。请先恢复后再执行。"
    dry_run_override = inp.get("dry_run")
    run_result = scheduler.run_goal_now(goal_id, dry_run_override=dry_run_override)
    if run_result is None:
        return f"执行失败，未返回运行结果。goal_id={goal_id}"
    lines = [
        f"执行完成: {run_result.summary}",
        f"状态: {'success' if run_result.success else 'failed'}",
        f"Run ID: {run_result.record.run_id}",
    ]
    if run_result.record.error:
        lines.append(f"错误: {run_result.record.error}")
    return "\n".join(lines)


def _tool_create_goal(inp: dict) -> str:
    import goals as goals_mod
    import scheduler
    from loops import discover

    loop_name = inp.get("loop", "")
    loops = discover()
    if loop_name not in loops:
        return f"不支持的 loop 类型：{loop_name}，当前支持：{', '.join(loops)}"

    loop = loops[loop_name]
    trigger_mode = inp.get("trigger_mode", "cron")
    supported_modes = tuple(getattr(loop, "supported_trigger_modes", ("cron", "goal")))
    if trigger_mode not in supported_modes:
        return (
            f"loop {loop_name} 不支持触发模式 {trigger_mode}，"
            f"支持：{', '.join(supported_modes)}"
        )

    schedule = inp.get("schedule")
    if trigger_mode in ("cron", "goal") and not schedule:
        return f"{trigger_mode} 模式需要 schedule（cron 表达式），请补充。"
    if trigger_mode == "event" and schedule:
        return "event 模式不应包含 schedule。"

    goal = goals_mod.add(
        raw=inp.get("raw", ""),
        schedule=schedule,
        loop=loop_name,
        trigger_mode=trigger_mode,
        goal_condition=inp.get("goal_condition"),
        dry_run=bool(inp.get("dry_run", False)),
        retry_after_minutes=inp.get("retry_after_minutes") or 30,
        max_retries=inp.get("max_retries") or 3,
        retry_backoff_factor=inp.get("retry_backoff_factor") or 2,
        retry_max_minutes=inp.get("retry_max_minutes") or 240,
    )
    scheduler.add_goal(goal)
    mode_label = {"cron": "定时", "goal": "目标驱动", "event": "事件驱动"}.get(trigger_mode, trigger_mode)
    return (
        f"✓ 已创建 goal {goal['id']}：{inp.get('summary', inp.get('raw', ''))}\n"
        f"  触发模式: {mode_label} | Loop: {loop_name}\n"
        f"  Schedule: {goal.get('schedule')} | DryRun: {goal.get('dry_run', False)}"
    )


def _tool_update_goal_preferences(inp: dict, harness: "ChatHarness") -> str:
    import goals as goals_mod
    from main import _sanitize_preferences, _flatten_preference_keys

    goal_id = inp.get("goal_id", "")
    if not goals_mod.get(goal_id):
        return f"未找到 goal: {goal_id}"

    preferences = _sanitize_preferences(inp.get("preferences"))
    if not preferences:
        return "preferences 为空或格式不正确，key 只能是 content/delivery/behavior/format。"

    merged = harness.memory.merge_save_goal_memory(
        goal_id,
        {
            "preferences": preferences,
            "preferences_updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    pref_keys = _flatten_preference_keys(preferences)
    return f"已更新 {goal_id} 的偏好：{', '.join(pref_keys)}"


def _tool_update_loop_preferences(inp: dict, harness: "ChatHarness") -> str:
    from loops import discover
    from main import _sanitize_preferences, _flatten_preference_keys

    loop_name = inp.get("loop", "")
    if loop_name not in discover():
        return f"未找到 loop: {loop_name}"

    preferences = _sanitize_preferences(inp.get("preferences"))
    if not preferences:
        return "preferences 为空或格式不正确。"

    merged = harness.memory.merge_save_loop_memory(
        loop_name,
        {
            "preferences": preferences,
            "preferences_updated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )
    pref_keys = _flatten_preference_keys(preferences)
    return f"已更新 {loop_name} 的默认偏好：{', '.join(pref_keys)}"


def _tool_update_user_preferences(inp: dict, harness: "ChatHarness") -> str:
    from main import _sanitize_preferences, _flatten_preference_keys

    preferences = _sanitize_preferences(inp.get("preferences"))
    goal_nicknames = inp.get("goal_nicknames")

    if not preferences and not goal_nicknames:
        return "preferences 和 goal_nicknames 均为空，没有可更新的内容。"

    updates: dict = {}
    if preferences:
        updates["preferences"] = preferences
    if isinstance(goal_nicknames, dict) and goal_nicknames:
        updates["goal_nicknames"] = goal_nicknames

    merged = harness.memory.merge_save_user_memory(updates)
    parts = []
    if preferences:
        parts.append(f"偏好: {', '.join(_flatten_preference_keys(preferences))}")
    if goal_nicknames:
        parts.append(f"别名: {', '.join(f'{k}→{v}' for k, v in goal_nicknames.items())}")
    return f"已记住 {' | '.join(parts)}"
