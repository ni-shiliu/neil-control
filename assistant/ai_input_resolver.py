"""
ai_input_resolver — 专用于 `add` 命令的单轮 create_goal 解析。

CLI 自然语言聊天已由 ChatHarness agentic loop 接管（engine/chat.py）。
这里只保留 `add <描述>` 命令的解析逻辑：把用户描述解析成 create_goal 结构。
"""

from __future__ import annotations

import json

from claude_client import get_client, get_model


def _strip_code_fence(text: str) -> str:
    raw = text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
    return raw


def _summarize_goals(goals: list[dict]) -> str:
    if not goals:
        return "(empty)"
    lines = []
    for goal in goals[:20]:
        lines.append(
            f"- id={goal.get('id')} | loop={goal.get('loop')} | "
            f"status={goal.get('status')} | schedule={goal.get('schedule')} | raw={goal.get('raw')}"
        )
    return "\n".join(lines)


def _summarize_loops(loops: dict) -> str:
    lines = []
    for loop_name, loop in sorted(loops.items()):
        trigger_modes = ", ".join(getattr(loop, "supported_trigger_modes", ("cron",)))
        lines.append(
            f"- {loop_name}: {getattr(loop, 'description', '')} | trigger_modes={trigger_modes}"
        )
    return "\n".join(lines)


def _build_create_goal_prompt(description: str, *, goals: list[dict], loops: dict) -> str:
    return f"""你是 Neil Assistant 的 goal 创建解析器。
用户想添加一个新的自动化目标，请把描述解析成结构化的 create_goal。

用户描述：
{description}

当前已有 goals（供参考，避免重复）：
{_summarize_goals(goals)}

当前支持的 loops：
{_summarize_loops(loops)}

输出 JSON，格式如下：
{{
  "kind": "create_goal",
  "goal": {{
    "trigger_mode": "cron 或 goal 或 event",
    "schedule": "5字段cron；event 时为 null",
    "goal_condition": "goal 模式时填，其他为 null",
    "loop": "loop名称",
    "summary": "一句话摘要",
    "dry_run": false,
    "retry_after_minutes": null,
    "max_retries": null,
    "retry_backoff_factor": null,
    "retry_max_minutes": null
  }}
}}

如果描述不够明确（缺少时间或 loop 类型），返回：
{{
  "kind": "clarify",
  "reason": "missing_schedule 或 unsupported_request",
  "message": "一句简短说明"
}}

只输出 JSON，不要输出其他内容。
"""


def ai_resolve_input(
    description: str,
    *,
    goals: list[dict],
    loops: dict,
) -> dict:
    """解析 add 命令的目标描述，返回 create_goal 或 clarify。"""
    prompt = _build_create_goal_prompt(description, goals=goals, loops=loops)
    msg = get_client().messages.create(
        model=get_model(),
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = _strip_code_fence(msg.content[0].text)
    return json.loads(raw)
