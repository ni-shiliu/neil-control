"""渲染函数：只返回字符串，不 print。

从 main.py 原样抽出。这些函数是无状态纯函数，
接收 dict / 记录对象，产出可打印文本。
"""

from __future__ import annotations


def format_effect_statuses(effects: list[dict]) -> str:
    if not effects:
        return "0"

    counts: dict[str, int] = {}
    for effect in effects:
        status = effect.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))


def result_output_count(record: dict) -> int:
    outputs = record.get("result", {}).get("outputs", [])
    return len(outputs) if isinstance(outputs, list) else 0


def render_goal_row(goal: dict, *, show_status: bool = True) -> str:
    status_icon = "✓" if goal["status"] == "active" else "⏸"
    schedule_label = goal.get("schedule") or "-"
    status_part = f"{status_icon} {goal['status']:<6} " if show_status else ""
    row = f"{goal['id']:<15} {status_part}{goal['loop']:<20} {schedule_label:<15} {goal['raw']}"
    if goal.get("last_run"):
        row += f"\n  └─ 上次执行: {goal['last_run']}  结果: {goal.get('last_result', '')}"
    return row


def render_goal_list(goals: list[dict], title: str = "") -> str:
    if not goals:
        return title.replace("：", "暂无。") if title else "暂无目标。"
    header = f"\n{'ID':<15} {'状态':<8} {'Loop':<20} {'Cron':<15} {'原始描述'}\n" + "-" * 90
    rows = "\n".join(render_goal_row(g) for g in goals)
    return f"{header}\n{rows}\n"


def render_goal_numbered(goals: list[dict], title: str) -> str:
    if not goals:
        return f"当前没有{title}的 goal。"
    lines = [f"当前{title}的 goal："]
    for idx, g in enumerate(goals, 1):
        lines.append(
            f"{idx}. {g['id']} | status={g['status']} | loop={g['loop']} | "
            f"schedule={g.get('schedule') or '-'} | {g['raw']}"
        )
    return "\n".join(lines)


def render_goal_detail(goal: dict) -> str:
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


def render_run_list(records: list[dict]) -> str:
    if not records:
        return "暂无运行记录。"
    header = f"\n{'Run ID':<14} {'Loop':<22} {'状态':<10} {'DryRun':<8} {'开始时间'}\n" + "-" * 90
    rows = []
    for r in records:
        rows.append(
            f"{r.get('run_id', ''):<14} {r.get('loop_name', ''):<22} "
            f"{r.get('status', ''):<10} {str(r.get('dry_run', False)):<8} "
            f"{r.get('started_at', '')}\n  └─ {r.get('summary', '')}"
        )
    return f"{header}\n" + "\n".join(rows) + "\n"


def render_run_detail(record: dict) -> str:
    lines = [
        f"\nRun ID:      {record.get('run_id', '')}",
        f"Goal ID:     {record.get('goal_id', '')}",
        f"Loop:        {record.get('loop_name', '')}",
        f"状态:        {record.get('status', '')}",
        f"Dry Run:     {record.get('dry_run', False)}",
        f"开始:        {record.get('started_at', '')}",
        f"结束:        {record.get('ended_at', '')}",
        f"耗时(ms):    {record.get('duration_ms', '')}",
        f"Summary:     {record.get('summary', '')}",
        f"Outputs:     {result_output_count(record)}",
        f"Effects:     {len(record.get('planned_effects', []))} planned / {len(record.get('committed_effects', []))} recorded",
        f"Effect状态:  {format_effect_statuses(record.get('committed_effects', []))}",
        f"通知:        {len(record.get('notifications', []))}",
    ]
    if record.get("error"):
        lines.append(f"错误:        {record['error']}")
    return "\n".join(lines)


def render_rerun_result(run_result) -> str:
    rec = run_result.record
    lines = [
        f"完成: {run_result.summary}",
        f"状态: {'success' if run_result.success else 'failed'}",
        f"Run ID: {rec.run_id}",
        f"Effects: {len(rec.planned_effects)} planned / {len(rec.committed_effects)} recorded",
        f"Effect状态: {format_effect_statuses(rec.committed_effects)}",
        f"Outputs: {result_output_count(rec.to_dict())}",
    ]
    if rec.error:
        lines.append(f"错误: {rec.error}")
    return "\n".join(lines)
