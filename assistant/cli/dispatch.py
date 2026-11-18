"""命令分发中心 + 命令表单一来源。

`COMMANDS` 是全部命令的唯一定义处，completer 与 help 均从它派生，
消除原先 main.py 里命令表三处硬编码（_process_input / AssistantCompleter / _cmd_help）。

`dispatch()` 取代原 `_process_input`：解析首词并路由到 cli.commands 的 handler。
退出（exit/quit）不再在此处 sys.exit，而是返回 route="exit" 交给 REPL 层处理，
把「退出进程」从分发逻辑中剥离。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from cli import commands
from cli.context import CliContext
from harness.interaction import Interaction

@dataclass(frozen=True)
class Command:
    name: str
    help: str                       # help 文本里的一行说明
    completions: tuple[str, ...] = ()   # 该命令第 2+ 词的静态补全项（动态项在 completer 里另加）


# ── 命令表：唯一来源 ──────────────────────────────────────────────────────────
# 顺序即 help / 补全的展示顺序。
COMMANDS: tuple[Command, ...] = (
    Command("list", "查看所有目标"),
    Command("init", "初始化项目级 RUNTIME.md / loop 脚手架", ("loop", "loops", "--force", "--with-doc")),
    Command("add", "创建目标，可显式指定 dry-run / retry 策略",
            ("--dry-run", "--retry-after", "--max-retries", "--backoff", "--retry-max")),
    Command("goal", "查看某个 goal 的完整配置"),
    Command("pause", "暂停目标"),
    Command("resume", "恢复目标"),
    Command("delete", "删除目标"),
    Command("rerun", "立即执行一次 goal，可临时覆盖 dry-run", ("--dry-run",)),
    Command("runs", "查看最近 N 条运行记录（默认 10）"),
    Command("run", "查看某条运行记录详情"),
    Command("goalmem", "查看某个 goal 的 memory"),
    Command("loopmem", "查看某个 loop 的长期 memory"),
    Command("browser", "检查本机 Chrome 浏览器能力（browser doctor [--keep]）", ("doctor", "--keep")),
    Command("help", "显示帮助"),
    Command("exit", "退出"),
    Command("quit", "退出"),
)

COMMAND_NAMES: tuple[str, ...] = tuple(c.name for c in COMMANDS)
_COMMAND_BY_NAME: dict[str, Command] = {c.name: c for c in COMMANDS}

# 需要 goal_id 作为第 2 词补全的命令
GOAL_ARG_COMMANDS = frozenset({"goal", "pause", "resume", "delete", "rerun", "goalmem"})
# 需要 loop_name 作为第 2 词补全的命令
LOOP_ARG_COMMANDS = frozenset({"loopmem"})


def _empty_result() -> dict:
    return {"route": "empty", "command": None, "ai_result": None, "execution": {"executed": False}}


def dispatch(ctx: CliContext, user_input: str) -> Interaction:
    """CLI 命令/自然语言的统一类型化输出。"""
    return Interaction.coerce(_dispatch(ctx, user_input))


def _dispatch(ctx: CliContext, user_input: str) -> Interaction | dict:
    """解析并执行一条 CLI 输入；外层 ``dispatch`` 统一适配为 Interaction。"""
    parts = user_input.strip().split()
    if not parts:
        return _empty_result()

    cmd = parts[0].lower()

    # 无参数命令
    if cmd == "list":
        commands.cmd_list()
        return commands.cmd_result(cmd)
    if cmd in ("help", "?"):
        print(render_help())
        return commands.cmd_result("help")

    # 需要第一个参数的命令：name -> (fn, usage)
    arg1_cmds: dict[str, tuple[Callable[[str], None], str]] = {
        "goal":    (commands.cmd_goal,    "goal <goal_id>"),
        "run":     (lambda a: commands.cmd_run(ctx, a),     "run <run_id>"),
        "goalmem": (lambda a: commands.cmd_goalmem(ctx, a), "goalmem <goal_id>"),
        "loopmem": (lambda a: commands.cmd_loopmem(ctx, a), "loopmem <loop_name>"),
        "pause":   (commands.cmd_pause,   "pause <goal_id>"),
        "resume":  (commands.cmd_resume,  "resume <goal_id>"),
        "delete":  (commands.cmd_delete,  "delete <goal_id>"),
    }
    if cmd in arg1_cmds:
        fn, usage = arg1_cmds[cmd]
        if len(parts) < 2:
            print(f"用法：{usage}")
            return commands.cmd_result(cmd, executed=False)
        fn(parts[1])
        return commands.cmd_result(cmd)

    # runs 命令（参数可选）
    if cmd == "runs":
        limit = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 10
        commands.cmd_runs(ctx, limit)
        return commands.cmd_result(cmd)

    # 需要子命令解析的命令
    if cmd == "init":
        return commands.handle_init(ctx, user_input, cmd)
    if cmd == "add":
        return commands.handle_add(user_input, cmd)
    if cmd == "rerun":
        return commands.handle_rerun(user_input, cmd)
    if cmd == "browser":
        return commands.handle_browser(user_input, cmd)
    if cmd == "chrome":
        return commands.handle_chrome(user_input, cmd)

    # 退出：不在此处 sys.exit，交给 REPL 层
    if cmd in ("exit", "quit"):
        return {"route": "exit", "command": cmd, "ai_result": None,
                "execution": {"executed": True, "kind": "command"}}

    # 自然语言只交给 Harness 门面；渠道适配、路由与后续层完全封装。
    return ctx.harness.handle(
        channel=ctx.channel_id, raw_text=user_input,
        identity=getattr(ctx, "request_identity", None),
    )


def render_help() -> str:
    """help 文本从命令表派生（不含 chrome 这类隐藏命令，保持原 help 内容风格）。"""
    lines = ["", "命令列表："]
    for c in COMMANDS:
        if c.name == "quit":  # exit/quit 合并成一行展示
            continue
        label = "exit / quit" if c.name == "exit" else c.name
        lines.append(f"  {label:<22}{c.help}")
    lines.append("")
    lines.append("直接输入自然语言来添加新目标或对话，例如：")
    lines.append("  每天早上10点帮我处理邮件")
    lines.append("  每天11点半提醒我该去接水了")
    lines.append("")
    lines.append("add 示例：")
    lines.append("  add --dry-run 每天早上8点给我发每日简报")
    lines.append("  add --retry-after 10 --max-retries 5 --backoff 2 有新邮件时立刻处理")
    lines.append("")
    lines.append("init 示例：")
    lines.append("  init / init --force / init loop my_loop [--with-doc] / init loops")
    lines.append("")
    lines.append("browser 示例：")
    lines.append("  browser doctor [--keep]")
    return "\n".join(lines)
