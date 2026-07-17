"""CLI 命令实现：cmd_*（执行）、handle_*（解析+调度）、parse_*（参数解析）。

从 main.py 原样抽出。所有需要运行时依赖（recorder/memory/chat runtime/路径）
的函数都接收 CliContext，取代原先对模块级单例的直接引用。
行为与重构前逐字一致。
"""

from __future__ import annotations

import json
import shlex

import goals as goals_mod
import scheduler
from loops import discover

from cli.context import CliContext
from cli.render import (
    render_goal_detail,
    render_goal_list,
    render_goal_numbered,
    render_rerun_result,
    render_run_detail,
    render_run_list,
)
from cli.templates import (
    loop_doc_template,
    loop_py_template,
    loop_test_template,
    runtime_doc_template,
    write_template,
)


# ── 通用返回构造 ─────────────────────────────────────────────────────────────

def cmd_result(cmd: str, executed: bool = True) -> dict:
    return {"route": "command", "command": cmd, "ai_result": None,
            "execution": {"executed": executed, "kind": "command"}}


# ── init ────────────────────────────────────────────────────────────────────

def _parse_force_flag(tokens: list[str]) -> bool:
    return "--force" in tokens


def _parse_with_doc_flag(tokens: list[str]) -> bool:
    return "--with-doc" in tokens


def parse_init_command(user_input: str) -> tuple[str, dict] | None:
    try:
        tokens = shlex.split(user_input)
    except ValueError as e:
        print(f"命令解析失败: {e}")
        return None

    force = _parse_force_flag(tokens[1:])
    with_doc = _parse_with_doc_flag(tokens[1:])
    known_flags = {"--force", "--with-doc"}
    non_flags = [token for token in tokens[1:] if not token.startswith("--")]

    for token in tokens[1:]:
        if token.startswith("--") and token not in known_flags:
            print(f"init 不支持参数 {token}。")
            return None

    if not non_flags:
        if with_doc:
            print("init --with-doc 只能用于 init loop <name>。")
            return None
        return "root", {"force": force}

    if non_flags[0] == "loops":
        if len(non_flags) > 1:
            print("init loops 不接受额外参数。")
            return None
        if with_doc:
            print("init loops 不需要 --with-doc。")
            return None
        return "loops", {"force": force}

    if non_flags[0] == "loop":
        if len(non_flags) < 2:
            print("init loop 命令缺少 loop_name。")
            return None
        if len(non_flags) > 2:
            print("init loop 只能指定一个 loop_name。")
            return None
        return "loop", {"force": force, "with_doc": with_doc, "loop_name": non_flags[1]}

    print("不支持的 init 命令。可用：init / init loop <name> / init loops")
    return None


def cmd_init(ctx: CliContext, force: bool = False) -> None:
    status = write_template(ctx.runtime_doc, runtime_doc_template(), force=force)
    if status == "skipped":
        print(f"已存在: {ctx.runtime_doc.name}，如需覆盖请使用 init --force")
        return
    print(f"{'已覆盖' if force and status == 'updated' else '已创建'} {ctx.runtime_doc.name}")


def cmd_init_loop(ctx: CliContext, loop_name: str, *, force: bool = False, with_doc: bool = False) -> None:
    py_path = ctx.loops_dir / f"{loop_name}.py"
    md_path = ctx.loops_dir / f"{loop_name}.md"
    test_path = ctx.tests_dir / f"test_{loop_name}.py"

    discovered = discover()
    loop = discovered.get(loop_name)
    should_create_doc = with_doc or bool(getattr(loop, "use_loop_doc", False))

    py_status = "skipped"
    if loop is None:
        py_status = write_template(py_path, loop_py_template(loop_name), force=force)

    md_status = "disabled"
    if should_create_doc:
        md_status = write_template(md_path, loop_doc_template(loop_name, loop), force=force)
    test_status = write_template(test_path, loop_test_template(loop_name), force=force)

    print(f"loop 初始化完成: {loop_name}")
    if loop is None:
        print(f"  py:   {py_status}  {ctx.loops_dir.name}/{loop_name}.py")
    else:
        print(f"  py:   已存在  {ctx.loops_dir.name}/{loop_name}.py")
    if should_create_doc:
        print(f"  md:   {md_status}  {ctx.loops_dir.name}/{loop_name}.md")
    else:
        print(f"  md:   disabled  {ctx.loops_dir.name}/{loop_name}.md  (可用 --with-doc 显式创建)")
    print(f"  test: {test_status}  {ctx.tests_dir.name}/test_{loop_name}.py")


def cmd_init_loops(ctx: CliContext, force: bool = False) -> None:
    loops = discover()
    if not loops:
        print("当前没有可初始化的 loop。")
        return

    created = 0
    updated = 0
    skipped = 0
    disabled = 0
    for loop_name, loop in sorted(loops.items()):
        md_path = ctx.loops_dir / f"{loop_name}.md"
        if not getattr(loop, "use_loop_doc", False) and not md_path.exists():
            disabled += 1
            continue
        status = write_template(md_path, loop_doc_template(loop_name, loop), force=force)
        if status == "created":
            created += 1
        elif status == "updated":
            updated += 1
        else:
            skipped += 1

    print("loops 初始化完成")
    print(f"  created: {created}")
    print(f"  updated: {updated}")
    print(f"  skipped: {skipped}")
    print(f"  disabled: {disabled}")


def handle_init(ctx: CliContext, user_input: str, cmd: str) -> dict:
    parsed = parse_init_command(user_input)
    if not parsed:
        return cmd_result(cmd, executed=False)
    mode, options = parsed
    if mode == "root":
        cmd_init(ctx, force=options["force"])
    elif mode == "loop":
        cmd_init_loop(ctx, options["loop_name"], force=options["force"], with_doc=options["with_doc"])
    elif mode == "loops":
        cmd_init_loops(ctx, force=options["force"])
    return cmd_result(cmd)


# ── goal 列表 / 详情 / 运行记录 ───────────────────────────────────────────────

def cmd_list() -> None:
    print(render_goal_list(goals_mod.list_all()))


def cmd_list_active_goals() -> None:
    goals = [g for g in goals_mod.list_all() if g.get("status") == "active"]
    print(render_goal_numbered(goals, "运行中"))


def cmd_list_paused_goals() -> None:
    goals = [g for g in goals_mod.list_all() if g.get("status") == "paused"]
    print(render_goal_numbered(goals, "暂停"))


def cmd_list_all_goals() -> None:
    print(render_goal_numbered(goals_mod.list_all(), "所有"))


def cmd_runs(ctx: CliContext, limit: int = 10) -> None:
    print(render_run_list(ctx.recorder.list_recent(limit=limit)))


def cmd_goal(goal_id: str) -> None:
    goal = goals_mod.get(goal_id)
    print(render_goal_detail(goal) if goal else f"未找到目标 {goal_id}")


def cmd_run(ctx: CliContext, run_id: str) -> None:
    record = ctx.recorder.find_run(run_id)
    print(render_run_detail(record) if record else f"未找到运行记录 {run_id}")


def cmd_goalmem(ctx: CliContext, goal_id: str) -> None:
    data = ctx.memory.load_goal_memory(goal_id)
    if not data:
        print(f"goal {goal_id} 暂无 memory。")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_loopmem(ctx: CliContext, loop_name: str) -> None:
    data = ctx.memory.load_loop_memory(loop_name)
    if not data:
        print(f"loop {loop_name} 暂无 memory。")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


# ── rerun ─────────────────────────────────────────────────────────────────────

def parse_rerun_command(user_input: str) -> tuple[dict, str] | None:
    try:
        tokens = shlex.split(user_input)
    except ValueError as e:
        print(f"命令解析失败: {e}")
        return None

    options = {"dry_run": None}
    goal_id = ""
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token == "--dry-run":
            options["dry_run"] = True
            i += 1
        elif token.startswith("--"):
            print(f"rerun 不支持参数 {token}。")
            return None
        else:
            if goal_id:
                print("rerun 命令只能指定一个 goal_id。")
                return None
            goal_id = token
            i += 1

    if goal_id and i < len(tokens):
        print("rerun 命令只能指定一个 goal_id。")
        return None

    if not goal_id:
        print("rerun 命令缺少 goal_id。")
        return None
    return options, goal_id


def cmd_rerun(goal_id: str, *, dry_run_override: bool | None = None) -> None:
    goal = goals_mod.get(goal_id)
    if not goal:
        print(f"未找到目标 {goal_id}")
        return
    if goal.get("status") != "active":
        print(f"goal {goal_id} 当前为 {goal.get('status')} 状态，不能执行。请先 resume 后再 rerun。")
        return
    dry_run_label = dry_run_override if dry_run_override is not None else goal.get("dry_run", False)
    print(f"立即执行 {goal_id} ... (dry_run={dry_run_label})")
    run_result = scheduler.run_goal_now(goal_id, dry_run_override=dry_run_override)
    print(render_rerun_result(run_result) if run_result else "执行失败，未返回运行结果。")


def handle_rerun(user_input: str, cmd: str) -> dict:
    parsed = parse_rerun_command(user_input)
    if not parsed:
        return cmd_result(cmd, executed=False)
    options, goal_id = parsed
    cmd_rerun(goal_id, dry_run_override=options.get("dry_run"))
    return cmd_result(cmd)


# ── add / 创建 goal ───────────────────────────────────────────────────────────

def parse_add_command(user_input: str) -> tuple[dict, str] | None:
    try:
        tokens = shlex.split(user_input)
    except ValueError as e:
        print(f"命令解析失败: {e}")
        return None

    options = {
        "dry_run": False,
        "retry_after_minutes": None,
        "max_retries": None,
        "retry_backoff_factor": None,
        "retry_max_minutes": None,
    }
    description_parts: list[str] = []
    value_options = {
        "--retry-after": "retry_after_minutes",
        "--max-retries": "max_retries",
        "--backoff": "retry_backoff_factor",
        "--retry-max": "retry_max_minutes",
    }

    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token == "--dry-run":
            options["dry_run"] = True
            i += 1
        elif token in value_options:
            if i + 1 >= len(tokens) or tokens[i + 1].startswith("--"):
                print(f"add 参数 {token} 缺少值。")
                return None
            try:
                options[value_options[token]] = int(tokens[i + 1])
            except ValueError:
                print(f"add 参数 {token} 的值必须是整数。")
                return None
            i += 2
        else:
            description_parts = tokens[i:]
            break

    description = " ".join(description_parts).strip()
    if not description:
        print("add 命令缺少目标描述。")
        return None
    return options, description


def create_goal_from_result(user_input: str, result: dict, overrides: dict | None = None) -> None:
    loop_name = result.get("loop", "")
    loops = discover()
    if loop_name not in loops:
        print(f"暂不支持的 loop 类型：{loop_name}，当前支持：{', '.join(loops)}")
        return
    loop = loops[loop_name]

    overrides = overrides or {}
    trigger_mode = result.get("trigger_mode", "cron")
    supported_modes = tuple(getattr(loop, "supported_trigger_modes", ("cron", "goal")))
    if trigger_mode not in supported_modes:
        print(
            f"loop {loop_name} 不支持触发模式 {trigger_mode}，"
            f"支持：{', '.join(supported_modes)}。"
        )
        if trigger_mode == "event" and "cron" in supported_modes:
            print("这类任务通常需要明确时间，例如：每天早上8点给我发每日简报。")
        return

    schedule = result.get("schedule")
    if trigger_mode in ("cron", "goal") and not schedule:
        print(f"{trigger_mode} 模式需要 schedule，但这次解析没有得到时间表达。")
        print("请补充明确时间，例如：每天早上8点给我发每日简报。")
        return

    if trigger_mode == "event" and schedule:
        print("event 模式不应包含 schedule，请改成事件触发描述，例如：有新邮件时立刻处理。")
        return

    goal = goals_mod.add(
        raw=user_input,
        schedule=schedule,
        loop=loop_name,
        trigger_mode=trigger_mode,
        goal_condition=result.get("goal_condition"),
        dry_run=bool(overrides.get("dry_run", result.get("dry_run", False))),
        retry_after_minutes=overrides.get("retry_after_minutes") or result.get("retry_after_minutes") or 30,
        max_retries=overrides.get("max_retries") or result.get("max_retries") or 3,
        retry_backoff_factor=overrides.get("retry_backoff_factor") or result.get("retry_backoff_factor") or 2,
        retry_max_minutes=overrides.get("retry_max_minutes") or result.get("retry_max_minutes") or 240,
    )
    scheduler.add_goal(goal)
    mode_label = {"cron": "定时", "goal": "目标驱动", "event": "事件驱动"}.get(trigger_mode, trigger_mode)
    print(f"✓ 已添加目标 {goal['id']}：{result['summary']}")
    print(f"  触发模式: {mode_label} | Loop: {goal['loop']}")
    print(f"  调度: {goal['schedule']} | DryRun: {goal.get('dry_run', False)}")
    print(
        f"  Retry: after={goal.get('retry_after_minutes')}m "
        f"max={goal.get('max_retries')} "
        f"backoff={goal.get('retry_backoff_factor')} "
        f"cap={goal.get('retry_max_minutes')}m"
    )


_CLARIFY_MESSAGES = {
    "missing_goal_target": "我还不能确定你要操作哪个 goal。请直接给我 goal_id，或者补充更具体的描述。",
    "ambiguous_goal_target": "我还不能唯一定位目标。请直接给我 goal_id，或者补充更具体的描述。",
    "missing_schedule": "我理解你是在创建目标，但还缺少明确时间。请补充具体时间。",
    "unsupported_request": "这条输入我还不能稳定执行。请换一种更具体的说法。",
    "unclear_request": "我还不能稳定理解这条输入。请换一种更明确的说法。",
}


def handle_add(user_input: str, cmd: str) -> dict:
    parsed = parse_add_command(user_input)
    if not parsed:
        return cmd_result(cmd, executed=False)
    overrides, description = parsed
    try:
        from ai_input_resolver import ai_resolve_input
        result = ai_resolve_input(description, goals=goals_mod.list_all(), loops=discover())
    except Exception as e:
        print(f"解析失败: {e}")
        return {"route": "command", "command": cmd, "ai_result": None,
                "execution": {"executed": False, "kind": "command", "reason": "resolve_failed"}}
    if result.get("kind") != "create_goal":
        msg_map = {
            "clarify": result.get("message") or _CLARIFY_MESSAGES["missing_schedule"],
            "chat": result.get("message", "我不太理解，请重新描述。"),
        }
        print(msg_map.get(result.get("kind"), _CLARIFY_MESSAGES["unsupported_request"]))
        return {"route": "command_ai", "command": cmd, "ai_result": result,
                "execution": {"executed": False, "kind": result.get("kind", "unknown")}}
    create_goal_from_result(description, result.get("goal") or {}, overrides=overrides)
    return {"route": "command_ai", "command": cmd, "ai_result": result,
            "execution": {"executed": True, "kind": "create_goal",
                          "loop": (result.get("goal") or {}).get("loop")}}


# ── pause / resume / delete（含 *_all）──────────────────────────────────────────

def cmd_pause(goal_id: str) -> None:
    if goals_mod.pause(goal_id):
        scheduler.pause_goal(goal_id)
        print(f"已暂停 {goal_id}")
    else:
        print(f"未找到目标 {goal_id}")


def cmd_resume(goal_id: str) -> None:
    if goals_mod.resume(goal_id):
        scheduler.resume_goal(goal_id)
        print(f"已恢复 {goal_id}")
    else:
        print(f"未找到目标 {goal_id}")


def cmd_delete(goal_id: str) -> None:
    confirm = input(f"确认删除 {goal_id}？(y/N) ").strip().lower()
    if confirm != "y":
        print("已取消")
        return
    if goals_mod.delete(goal_id):
        scheduler.remove_goal(goal_id)
        print(f"已删除 {goal_id}")
    else:
        print(f"未找到目标 {goal_id}")


def cmd_delete_all_goals() -> None:
    goals = goals_mod.list_all()
    if not goals:
        print("当前没有 goal。")
        return

    confirm = input(f"确认删除全部 {len(goals)} 个 goal？(y/N) ").strip().lower()
    if confirm != "y":
        print("已取消")
        return

    for goal in goals:
        scheduler.remove_goal(goal["id"])

    deleted = goals_mod.delete_all()
    if deleted:
        print(f"已删除全部 {deleted} 个 goal。")
    else:
        print("删除失败，请检查 goals.json 是否可写。")


def cmd_pause_all_goals() -> None:
    goals = goals_mod.list_all()
    if not goals:
        print("当前没有 goal。")
        return
    changed = goals_mod.pause_all()
    for goal in goals:
        scheduler.pause_goal(goal["id"])
    print(f"已暂停 {changed} 个 goal。")


def cmd_resume_all_goals() -> None:
    goals = goals_mod.list_all()
    if not goals:
        print("当前没有 goal。")
        return
    changed = goals_mod.resume_all()
    for goal in goals_mod.list_all():
        scheduler.resume_goal(goal["id"])
    print(f"已恢复 {changed} 个 goal。")


# ── browser / chrome ──────────────────────────────────────────────────────────

def handle_browser(user_input: str, cmd: str) -> dict:
    try:
        tokens = shlex.split(user_input)
    except ValueError as e:
        print(f"命令解析失败: {e}")
        return cmd_result(cmd, executed=False)

    if len(tokens) < 2 or tokens[1] != "doctor":
        print("用法：browser doctor [--keep]")
        return cmd_result(cmd, executed=False)

    keep = False
    for token in tokens[2:]:
        if token == "--keep":
            keep = True
        else:
            print(f"browser doctor 不支持参数 {token}。")
            return cmd_result(cmd, executed=False)

    from harness.agents.tools.browser.diagnostics import render_browser_diagnostic, run_browser_diagnostic

    print(render_browser_diagnostic(run_browser_diagnostic(keep=keep)))
    return cmd_result(cmd)


def extract_url(text: str) -> str | None:
    import re
    match = re.search(r"https?://[^\s，。；,;]+", text)
    return match.group(0) if match else None


def cmd_open_browser_url(url: str) -> None:
    from harness.agents.tools.browser.actions import open_url

    result = open_url(url)
    if not result.ok:
        print(f"打开失败：{result.message}")
        return
    state = result.state
    print(f"已在 Chrome 打开：{state.title if state else '(无标题)'} | {state.url if state else url}")


def handle_chrome(user_input: str, cmd: str) -> dict:
    url = extract_url(user_input)
    if not url:
        print("用法：chrome 打开 https://example.com")
        return cmd_result(cmd, executed=False)
    cmd_open_browser_url(url)
    return cmd_result(cmd)
