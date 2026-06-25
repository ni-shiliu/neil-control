"""
命令行入口。

自然语言输入 → Claude 解析为 goal（schedule + loop 类型）→ 存储 + 注册调度
管理命令：list / pause <id> / resume <id> / delete <id> / help
"""

import json
import logging
import shlex
import io
import sys
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from conversation_records import ConversationRecorder
from engine.chat import ChatHarness
from dotenv import load_dotenv
try:
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.history import FileHistory
except Exception:  # pragma: no cover - fallback for minimal envs
    pt_prompt = None
    AutoSuggestFromHistory = None
    Completer = object
    Completion = None
    FileHistory = None

import goals as goals_mod
import scheduler
import notifier
from engine.memory import MemoryStore
from engine.records import RunRecorder
from loops import discover

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(Path(__file__).parent / "assistant.log"),
    ],
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
_recorder = RunRecorder()
_memory = MemoryStore()
_conversation_recorder = ConversationRecorder()
_chat_harness = ChatHarness(memory=_memory, conversation_recorder=_conversation_recorder)
_ASSISTANT_DIR = Path(__file__).parent
_LOOPS_DIR = _ASSISTANT_DIR / "loops"
_TESTS_DIR = _ASSISTANT_DIR / "tests"
_RUNTIME_DOC = _ASSISTANT_DIR / "RUNTIME.md"
_CLI_HISTORY = _ASSISTANT_DIR / ".cli_history"
_BANNER_FILE = _ASSISTANT_DIR / "BANNER.txt"
_PREFERENCE_BUCKETS = {"content", "delivery", "behavior", "format"}


class _TeeStdout:
    def __init__(self, *targets):
        self.targets = targets

    def write(self, text: str) -> int:
        for target in self.targets:
            target.write(text)
        return len(text)

    def flush(self) -> None:
        for target in self.targets:
            target.flush()


def _format_effect_statuses(effects: list[dict]) -> str:
    if not effects:
        return "0"

    counts: dict[str, int] = {}
    for effect in effects:
        status = effect.get("status", "unknown")
        counts[status] = counts.get(status, 0) + 1
    return ", ".join(f"{status}={count}" for status, count in sorted(counts.items()))


def _result_output_count(record: dict) -> int:
    outputs = record.get("result", {}).get("outputs", [])
    return len(outputs) if isinstance(outputs, list) else 0


def _snake_to_camel(name: str) -> str:
    return "".join(part.capitalize() for part in name.split("_") if part)


def _current_goal_ids() -> list[str]:
    return [goal.get("id", "") for goal in goals_mod.list_all() if goal.get("id")]


def _current_loop_names() -> list[str]:
    return sorted(discover().keys())


def _recent_conversations(limit: int = 10) -> list[dict]:
    return _conversation_recorder.list_recent(limit=limit)


def _normalize_lookup_text(text: str) -> str:
    return "".join(ch.lower() for ch in text.strip() if not ch.isspace())


def _flatten_preference_keys(data: dict, prefix: str = "") -> list[str]:
    keys: list[str] = []
    for key, value in data.items():
        current = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            keys.extend(_flatten_preference_keys(value, current))
        else:
            keys.append(current)
    return keys


def _sanitize_preferences(preferences: dict | None) -> dict | None:
    if not isinstance(preferences, dict):
        return None

    sanitized: dict = {}
    for key, value in preferences.items():
        if key not in _PREFERENCE_BUCKETS:
            continue
        if isinstance(value, dict):
            sanitized[key] = value
    return sanitized or None


def _default_banner() -> str:
    return r"""
 _   _      _ _
| \ | | ___(_) |
|  \| |/ _ \ | |
| |\  |  __/ | |
|_| \_|\___|_|_|
"""


def _load_banner() -> str:
    if _BANNER_FILE.exists():
        try:
            content = _BANNER_FILE.read_text(encoding="utf-8").strip("\n")
            if content.strip():
                return bytes(content, "utf-8").decode("unicode_escape")
        except OSError as e:
            log.warning(f"读取 BANNER.txt 失败，使用默认 banner: {e}")
        except UnicodeDecodeError as e:
            log.warning(f"BANNER.txt 转义解析失败，使用原始内容: {e}")
            return content
    return _default_banner().strip("\n")


def _print_startup_banner() -> None:
    goals = goals_mod.list_all()
    active_goals = sum(1 for goal in goals if goal.get("status") == "active")
    loops = _current_loop_names()

    print(_load_banner())
    print("NEIL_ASSISTANT")
    print("  Personal Loop Runtime for Goals, Memory and Automation")
    print(f"  loops: {len(loops)} loaded")
    print(f"  goals: {active_goals} active / {len(goals)} total")
    print(f"  runtime doc: {'enabled' if _RUNTIME_DOC.exists() else 'disabled'}")
    print("  help: 输入 help 查看命令\n")


class AssistantCompleter(Completer):

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        stripped = text.lstrip()
        parts = stripped.split()
        word = document.get_word_before_cursor(WORD=True)

        commands = [
            "list", "init", "goal", "pause", "resume", "delete",
            "rerun", "runs", "run", "goalmem", "loopmem",
            "browser", "add", "help", "exit", "quit",
        ]
        if not parts:
            for cmd in commands:
                if cmd.startswith(word):
                    yield Completion(cmd, start_position=-len(word))
            return

        if len(parts) == 1 and not stripped.endswith(" "):
            for cmd in commands:
                if cmd.startswith(parts[0]):
                    yield Completion(cmd, start_position=-len(word))
            return

        cmd = parts[0].lower()
        goal_ids = _current_goal_ids()
        loop_names = _current_loop_names()

        if cmd == "init":
            choices = ["loop", "loops", "--force"]
            if len(parts) >= 2 and parts[1] == "loop":
                choices = loop_names + ["--with-doc", "--force"]
            for item in choices:
                if item.startswith(word):
                    yield Completion(item, start_position=-len(word))
            return

        if cmd in {"goal", "pause", "resume", "delete", "rerun", "goalmem"}:
            for goal_id in goal_ids:
                if goal_id.startswith(word):
                    yield Completion(goal_id, start_position=-len(word))
            if cmd == "rerun" and "--dry-run".startswith(word):
                yield Completion("--dry-run", start_position=-len(word))
            return

        if cmd == "loopmem":
            for loop_name in loop_names:
                if loop_name.startswith(word):
                    yield Completion(loop_name, start_position=-len(word))
            return

        if cmd == "add":
            for option in ("--dry-run", "--retry-after", "--max-retries", "--backoff", "--retry-max"):
                if option.startswith(word):
                    yield Completion(option, start_position=-len(word))
            return

        if cmd == "browser":
            for item in ("doctor", "--keep"):
                if item.startswith(word):
                    yield Completion(item, start_position=-len(word))
            return


def _runtime_doc_template() -> str:
    return """# Runtime Preferences

## 目标

- 这是当前用户自己的可选运行偏好文件
- 只有在用户主动创建后，runtime 才会加载它
- 这里更适合写个人偏好、全局约束、长期习惯，而不是某次运行的临时状态

## 全局原则

- 运行时状态放在 JSON：goal memory、loop memory、run records
- 用户自定义的长期偏好可以放在 Markdown
- Loop 只声明 effect，不直接做不可回放的副作用
- Output 是产物，Effect 是动作，两者分开

## 可选文档层

### 用户级偏好（可选）

- 当前文件是用户自己的全局偏好入口
- 不同用户可以完全不同，也可以不存在

### Loop 级长期知识（可选）

- 只给复杂 loop 单独建 `loops/<loop_name>.md`
- 简单 loop 优先只依赖代码注释，必要时再补文档
- loop 文档记录该 loop 的目标、边界、长期策略、人工确认规则

### 运行时记忆

- `memory/loops/*.json`：跨 goal 的长期状态
- `memory/goals/*.json`：goal 级短期状态
- `run_records/*.json`：结构化运行记录

## 开发约束

- 如果用户需要全局偏好文件，可以执行 `init`
- 新增 loop 时优先执行 `init loop <loop_name>`
- 只有复杂 loop 才建议补单独的 `.md`
- 修改规则时，优先更新对应的 Markdown，再调整代码
"""


def _loop_doc_template(loop_name: str, loop=None) -> str:
    description = getattr(loop, "description", "一句话描述这个 loop 负责什么")
    required_tools = getattr(loop, "required_tools", [])
    trigger_modes = getattr(loop, "supported_trigger_modes", ("cron", "goal"))
    tools_text = ", ".join(required_tools) if required_tools else "无"
    trigger_text = ", ".join(trigger_modes) if trigger_modes else "未声明"
    return f"""# {loop_name}

## 目标

- {description}

## 触发方式

- 支持模式：{trigger_text}

## 工具依赖

- {tools_text}

## 输入输出

- 输入：goal 配置、ctx.memory、ctx.goal_memory、ctx.recent_runs
- 输出：结构化 result，可选 `result.outputs`

## 长期规则

- 在这里记录这个 loop 的长期稳定策略
- 只写人工确认过的规则，不写一次性运行结果

## Memory 边界

- loop memory：跨 goal 共用的稳定经验
- goal memory：某个 goal 的短期状态
- recent runs：最近运行轨迹，只做辅助上下文

## 注意事项

- 副作用通过 effect 提交
- 不要把高频临时数据写进 Markdown
"""


def _loop_py_template(loop_name: str) -> str:
    class_name = _snake_to_camel(loop_name)
    return f'''from loops.base import BaseLoop


class {class_name}(BaseLoop):
    name = "{loop_name}"
    description = "一句话描述这个 loop 的职责"
    required_tools = []
    supported_trigger_modes = ("cron", "goal")

    def plan(self, goal, ctx=None):
        return {{}}

    def execute(self, context, ctx=None):
        return {{}}

    def verify(self, result):
        return True, ""

    def fix(self, result, issues, ctx=None):
        return result

    def report(self, result):
        return "完成"
'''


def _loop_test_template(loop_name: str) -> str:
    class_name = _snake_to_camel(loop_name)
    return f'''from engine.engine import LoopEngine
from engine.memory import MemoryStore
from loops.{loop_name} import {class_name}


def test_{loop_name}_runs():
    loop = {class_name}()
    engine = LoopEngine(memory_store=MemoryStore())
    goal = {{
        "id": "test_{loop_name}",
        "raw": "test {loop_name}",
        "loop": "{loop_name}",
        "trigger_mode": "cron",
        "schedule": "0 8 * * *",
        "dry_run": True,
    }}

    result = engine.run(loop, goal)

    assert result.record.loop_name == "{loop_name}"
'''


def _write_template(path: Path, content: str, *, force: bool = False) -> str:
    existed = path.exists()
    if existed and not force:
        return "skipped"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return "updated" if existed else "created"


def _parse_force_flag(tokens: list[str]) -> bool:
    return "--force" in tokens


def _parse_with_doc_flag(tokens: list[str]) -> bool:
    return "--with-doc" in tokens


def _parse_init_command(user_input: str) -> tuple[str, dict] | None:
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


def _cmd_init(force: bool = False) -> None:
    status = _write_template(_RUNTIME_DOC, _runtime_doc_template(), force=force)
    if status == "skipped":
        print(f"已存在: {_RUNTIME_DOC.name}，如需覆盖请使用 init --force")
        return
    print(f"{'已覆盖' if force and status == 'updated' else '已创建'} {_RUNTIME_DOC.name}")


def _cmd_init_loop(loop_name: str, *, force: bool = False, with_doc: bool = False) -> None:
    py_path = _LOOPS_DIR / f"{loop_name}.py"
    md_path = _LOOPS_DIR / f"{loop_name}.md"
    test_path = _TESTS_DIR / f"test_{loop_name}.py"

    discovered = discover()
    loop = discovered.get(loop_name)
    should_create_doc = with_doc or bool(getattr(loop, "use_loop_doc", False))

    py_status = "skipped"
    if loop is None:
        py_status = _write_template(py_path, _loop_py_template(loop_name), force=force)

    md_status = "disabled"
    if should_create_doc:
        md_status = _write_template(md_path, _loop_doc_template(loop_name, loop), force=force)
    test_status = _write_template(test_path, _loop_test_template(loop_name), force=force)

    print(f"loop 初始化完成: {loop_name}")
    if loop is None:
        print(f"  py:   {py_status}  {_LOOPS_DIR.name}/{loop_name}.py")
    else:
        print(f"  py:   已存在  {_LOOPS_DIR.name}/{loop_name}.py")
    if should_create_doc:
        print(f"  md:   {md_status}  {_LOOPS_DIR.name}/{loop_name}.md")
    else:
        print(f"  md:   disabled  {_LOOPS_DIR.name}/{loop_name}.md  (可用 --with-doc 显式创建)")
    print(f"  test: {test_status}  {_TESTS_DIR.name}/test_{loop_name}.py")


def _cmd_init_loops(force: bool = False) -> None:
    loops = discover()
    if not loops:
        print("当前没有可初始化的 loop。")
        return

    created = 0
    updated = 0
    skipped = 0
    disabled = 0
    for loop_name, loop in sorted(loops.items()):
        md_path = _LOOPS_DIR / f"{loop_name}.md"
        if not getattr(loop, "use_loop_doc", False) and not md_path.exists():
            disabled += 1
            continue
        status = _write_template(md_path, _loop_doc_template(loop_name, loop), force=force)
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

# ── 渲染函数：只返回字符串，不 print ─────────────────────────────────────────

def _render_goal_row(goal: dict, *, show_status: bool = True) -> str:
    status_icon = "✓" if goal["status"] == "active" else "⏸"
    schedule_label = goal.get("schedule") or "-"
    status_part = f"{status_icon} {goal['status']:<6} " if show_status else ""
    row = f"{goal['id']:<15} {status_part}{goal['loop']:<20} {schedule_label:<15} {goal['raw']}"
    if goal.get("last_run"):
        row += f"\n  └─ 上次执行: {goal['last_run']}  结果: {goal.get('last_result', '')}"
    return row


def _render_goal_list(goals: list[dict], title: str = "") -> str:
    if not goals:
        return title.replace("：", "暂无。") if title else "暂无目标。"
    header = f"\n{'ID':<15} {'状态':<8} {'Loop':<20} {'Cron':<15} {'原始描述'}\n" + "-" * 90
    rows = "\n".join(_render_goal_row(g) for g in goals)
    return f"{header}\n{rows}\n"


def _render_goal_numbered(goals: list[dict], title: str) -> str:
    if not goals:
        return f"当前没有{title}的 goal。"
    lines = [f"当前{title}的 goal："]
    for idx, g in enumerate(goals, 1):
        lines.append(
            f"{idx}. {g['id']} | status={g['status']} | loop={g['loop']} | "
            f"schedule={g.get('schedule') or '-'} | {g['raw']}"
        )
    return "\n".join(lines)


def _render_goal_detail(goal: dict) -> str:
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


def _render_run_list(records: list[dict]) -> str:
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


def _render_run_detail(record: dict) -> str:
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
        f"Outputs:     {_result_output_count(record)}",
        f"Effects:     {len(record.get('planned_effects', []))} planned / {len(record.get('committed_effects', []))} recorded",
        f"Effect状态:  {_format_effect_statuses(record.get('committed_effects', []))}",
        f"通知:        {len(record.get('notifications', []))}",
    ]
    if record.get("error"):
        lines.append(f"错误:        {record['error']}")
    return "\n".join(lines)


# ── 命令函数：只做数据获取，统一 print(render()) ──────────────────────────────

def _cmd_list() -> None:
    print(_render_goal_list(goals_mod.list_all()))


def _cmd_list_active_goals() -> None:
    goals = [g for g in goals_mod.list_all() if g.get("status") == "active"]
    print(_render_goal_numbered(goals, "运行中"))


def _cmd_list_paused_goals() -> None:
    goals = [g for g in goals_mod.list_all() if g.get("status") == "paused"]
    print(_render_goal_numbered(goals, "暂停"))


def _cmd_list_all_goals() -> None:
    print(_render_goal_numbered(goals_mod.list_all(), "所有"))


def _cmd_runs(limit: int = 10) -> None:
    print(_render_run_list(_recorder.list_recent(limit=limit)))


def _cmd_goal(goal_id: str) -> None:
    goal = goals_mod.get(goal_id)
    print(_render_goal_detail(goal) if goal else f"未找到目标 {goal_id}")


def _cmd_run(run_id: str) -> None:
    record = _recorder.find_run(run_id)
    print(_render_run_detail(record) if record else f"未找到运行记录 {run_id}")


def _parse_rerun_command(user_input: str) -> tuple[dict, str] | None:
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


def _render_rerun_result(run_result) -> str:
    rec = run_result.record
    lines = [
        f"完成: {run_result.summary}",
        f"状态: {'success' if run_result.success else 'failed'}",
        f"Run ID: {rec.run_id}",
        f"Effects: {len(rec.planned_effects)} planned / {len(rec.committed_effects)} recorded",
        f"Effect状态: {_format_effect_statuses(rec.committed_effects)}",
        f"Outputs: {_result_output_count(rec.to_dict())}",
    ]
    if rec.error:
        lines.append(f"错误: {rec.error}")
    return "\n".join(lines)


def _cmd_rerun(goal_id: str, *, dry_run_override: bool | None = None) -> None:
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
    print(_render_rerun_result(run_result) if run_result else "执行失败，未返回运行结果。")


def _cmd_goalmem(goal_id: str) -> None:
    data = _memory.load_goal_memory(goal_id)
    if not data:
        print(f"goal {goal_id} 暂无 memory。")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _cmd_loopmem(loop_name: str) -> None:
    data = _memory.load_loop_memory(loop_name)
    if not data:
        print(f"loop {loop_name} 暂无 memory。")
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _parse_add_command(user_input: str) -> tuple[dict, str] | None:
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


def _create_goal_from_result(user_input: str, result: dict, overrides: dict | None = None) -> None:
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


def _cmd_pause(goal_id: str) -> None:
    if goals_mod.pause(goal_id):
        scheduler.pause_goal(goal_id)
        print(f"已暂停 {goal_id}")
    else:
        print(f"未找到目标 {goal_id}")


def _cmd_resume(goal_id: str) -> None:
    if goals_mod.resume(goal_id):
        scheduler.resume_goal(goal_id)
        print(f"已恢复 {goal_id}")
    else:
        print(f"未找到目标 {goal_id}")


def _cmd_delete(goal_id: str) -> None:
    confirm = input(f"确认删除 {goal_id}？(y/N) ").strip().lower()
    if confirm != "y":
        print("已取消")
        return
    if goals_mod.delete(goal_id):
        scheduler.remove_goal(goal_id)
        print(f"已删除 {goal_id}")
    else:
        print(f"未找到目标 {goal_id}")


def _cmd_delete_all_goals() -> None:
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


def _cmd_pause_all_goals() -> None:
    goals = goals_mod.list_all()
    if not goals:
        print("当前没有 goal。")
        return
    changed = goals_mod.pause_all()
    for goal in goals:
        scheduler.pause_goal(goal["id"])
    print(f"已暂停 {changed} 个 goal。")


def _cmd_resume_all_goals() -> None:
    goals = goals_mod.list_all()
    if not goals:
        print("当前没有 goal。")
        return
    changed = goals_mod.resume_all()
    for goal in goals_mod.list_all():
        scheduler.resume_goal(goal["id"])
    print(f"已恢复 {changed} 个 goal。")


def _goal_aliases(goal: dict) -> list[str]:
    loop_name = goal.get("loop", "")
    aliases = [goal.get("id", ""), goal.get("raw", ""), loop_name]
    if loop_name == "daily_briefing_loop":
        aliases.extend(["简报", "每日简报", "早报", "briefing"])
    if loop_name == "email_loop":
        aliases.extend(["邮件", "邮箱", "未读邮件", "邮件处理", "email", "mail"])
    return [alias for alias in aliases if alias]


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _goal_matches_today(goal: dict) -> bool:
    today = datetime.now().date().isoformat()
    last_run = goal.get("last_run") or ""
    created_at = goal.get("created_at") or ""
    return last_run.startswith(today) or created_at.startswith(today)


def _goal_sort_timestamp(goal: dict) -> float:
    dt = _parse_iso_datetime(goal.get("last_run")) or _parse_iso_datetime(goal.get("created_at"))
    return dt.timestamp() if dt else 0.0


def _resolve_goal_reference(goal_ref: str) -> list[dict]:
    normalized_ref = _normalize_lookup_text(goal_ref)
    if not normalized_ref:
        return []

    candidates: list[dict] = []
    for goal in goals_mod.list_all():
        aliases = [_normalize_lookup_text(alias) for alias in _goal_aliases(goal)]
        if any(normalized_ref in alias or alias in normalized_ref for alias in aliases if alias):
            candidates.append(goal)
    return candidates


def _rank_goal_candidates(candidates: list[dict], *, prefer_recent: bool = False, prefer_today: bool = False) -> list[dict]:
    def score(goal: dict) -> tuple[int, int, int, float]:
        recent_score = 1 if prefer_recent and (goal.get("last_run") or goal.get("created_at")) else 0
        today_score = 1 if prefer_today and _goal_matches_today(goal) else 0
        active_score = 1 if goal.get("status") == "active" else 0
        return (today_score, recent_score, active_score, _goal_sort_timestamp(goal))

    return sorted(candidates, key=score, reverse=True)


def _select_goal_from_ref(goal_ref: str, *, prefer_recent: bool = False, prefer_today: bool = False, deictic: bool = False) -> str | None:
    candidates = _resolve_goal_reference(goal_ref)
    if not candidates:
        return None

    ranked = _rank_goal_candidates(
        candidates,
        prefer_recent=prefer_recent or deictic,
        prefer_today=prefer_today,
    )
    if len(ranked) > 1:
        top = ranked[0]
        second = ranked[1]
        if _goal_sort_timestamp(top) != _goal_sort_timestamp(second) or top.get("status") != second.get("status"):
            return top["id"]
        return None
    return ranked[0]["id"]




def _execute_local_intent(name: str, slots: dict | None = None) -> bool:
    slots = slots or {}
    if name == "delete_all_goals":
        _cmd_delete_all_goals()
        return True
    if name == "pause_all_goals":
        _cmd_pause_all_goals()
        return True
    if name == "resume_all_goals":
        _cmd_resume_all_goals()
        return True
    if name == "list_all_goals":
        _cmd_list_all_goals()
        return True
    if name == "list_active_goals":
        _cmd_list_active_goals()
        return True
    if name == "list_paused_goals":
        _cmd_list_paused_goals()
        return True

    goal_id = slots.get("goal_id")
    goal_ref = slots.get("goal_ref")
    if not goal_id and goal_ref:
        goal_id = _select_goal_from_ref(
            goal_ref,
            prefer_recent=slots.get("prefer_recent") == "true",
            prefer_today=slots.get("prefer_today") == "true",
            deictic=slots.get("deictic") == "true",
        )
        if not goal_id:
            return False
    if name == "delete_goal" and goal_id:
        _cmd_delete(goal_id)
        return True
    if name == "pause_goal" and goal_id:
        _cmd_pause(goal_id)
        return True
    if name == "resume_goal" and goal_id:
        _cmd_resume(goal_id)
        return True
    if name == "rerun_goal" and goal_id:
        _cmd_rerun(goal_id)
        return True
    if name == "show_goal" and goal_id:
        _cmd_goal(goal_id)
        return True

    return False


_CLARIFY_MESSAGES = {
    "missing_goal_target": "我还不能确定你要操作哪个 goal。请直接给我 goal_id，或者补充更具体的描述。",
    "ambiguous_goal_target": "我还不能唯一定位目标。请直接给我 goal_id，或者补充更具体的描述。",
    "missing_schedule": "我理解你是在创建目标，但还缺少明确时间。请补充具体时间。",
    "unsupported_request": "这条输入我还不能稳定执行。请换一种更具体的说法。",
    "unclear_request": "我还不能稳定理解这条输入。请换一种更明确的说法。",
}


def _cmd_help() -> None:
    print("""
命令列表：
  list                  查看所有目标
  init [--force]        初始化项目级 RUNTIME.md
  init loop <name> [--force] [--with-doc]   初始化单个 loop 的 py/test，复杂 loop 才默认带 md
  init loops [--force]  扫描所有 loop，仅为已启用文档的 loop 批量初始化/更新说明
  add [选项] <描述>      创建目标，可显式指定 dry-run / retry 策略
  goal <goal_id>        查看某个 goal 的完整配置
  pause <goal_id>       暂停目标
  resume <goal_id>      恢复目标
  delete <goal_id>      删除目标
  rerun [--dry-run] <goal_id>  立即按当前配置执行一次 goal，可临时覆盖 dry-run
  runs [N]              查看最近 N 条运行记录（默认 10）
  run <run_id>          查看某条运行记录详情
  goalmem <goal_id>     查看某个 goal 的 memory
  loopmem <loop_name>   查看某个 loop 的长期 memory
  browser doctor [--keep]  检查本机 Chrome 浏览器能力
  help                  显示帮助
  exit / quit           退出

直接输入自然语言来添加新目标，例如：
  每天早上10点帮我处理邮件
  每天11点半提醒我该去接水了

add 示例：
  add --dry-run 每天早上8点给我发每日简报
  add --retry-after 10 --max-retries 5 --backoff 2 有新邮件时立刻处理

init 示例：
  init
  init --force
  init loop my_loop
  init loop my_loop --with-doc
  init loops

browser 示例：
  browser doctor
  browser doctor --keep
""")


def _cmd_result(cmd: str, executed: bool = True) -> dict:
    return {"route": "command", "command": cmd, "ai_result": None,
            "execution": {"executed": executed, "kind": "command"}}


def _handle_init(user_input: str, cmd: str) -> dict:
    parsed = _parse_init_command(user_input)
    if not parsed:
        return _cmd_result(cmd, executed=False)
    mode, options = parsed
    if mode == "root":
        _cmd_init(force=options["force"])
    elif mode == "loop":
        _cmd_init_loop(options["loop_name"], force=options["force"], with_doc=options["with_doc"])
    elif mode == "loops":
        _cmd_init_loops(force=options["force"])
    return _cmd_result(cmd)


def _handle_add(user_input: str, cmd: str) -> dict:
    parsed = _parse_add_command(user_input)
    if not parsed:
        return _cmd_result(cmd, executed=False)
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
    _create_goal_from_result(description, result.get("goal") or {}, overrides=overrides)
    return {"route": "command_ai", "command": cmd, "ai_result": result,
            "execution": {"executed": True, "kind": "create_goal",
                          "loop": (result.get("goal") or {}).get("loop")}}


def _handle_rerun(user_input: str, cmd: str) -> dict:
    parsed = _parse_rerun_command(user_input)
    if not parsed:
        return _cmd_result(cmd, executed=False)
    options, goal_id = parsed
    _cmd_rerun(goal_id, dry_run_override=options.get("dry_run"))
    return _cmd_result(cmd)


def _handle_browser(user_input: str, cmd: str) -> dict:
    try:
        tokens = shlex.split(user_input)
    except ValueError as e:
        print(f"命令解析失败: {e}")
        return _cmd_result(cmd, executed=False)

    if len(tokens) < 2 or tokens[1] != "doctor":
        print("用法：browser doctor [--keep]")
        return _cmd_result(cmd, executed=False)

    keep = False
    for token in tokens[2:]:
        if token == "--keep":
            keep = True
        else:
            print(f"browser doctor 不支持参数 {token}。")
            return _cmd_result(cmd, executed=False)

    from engine.tools.browser.diagnostics import render_browser_diagnostic, run_browser_diagnostic

    print(render_browser_diagnostic(run_browser_diagnostic(keep=keep)))
    return _cmd_result(cmd)


def _process_input(user_input: str) -> dict:
    parts = user_input.strip().split()
    if not parts:
        return {"route": "empty", "command": None, "ai_result": None, "execution": {"executed": False}}

    cmd = parts[0].lower()

    # 无参数命令
    simple: dict[str, object] = {
        "list":  _cmd_list,
        "help":  _cmd_help,
        "?":     _cmd_help,
    }
    if cmd in simple:
        simple[cmd]()
        return _cmd_result(cmd)

    # 需要第一个参数的命令
    arg1_cmds: dict[str, tuple] = {
        "goal":    (_cmd_goal,    "goal <goal_id>"),
        "run":     (_cmd_run,     "run <run_id>"),
        "goalmem": (_cmd_goalmem, "goalmem <goal_id>"),
        "loopmem": (_cmd_loopmem, "loopmem <loop_name>"),
        "pause":   (_cmd_pause,   "pause <goal_id>"),
        "resume":  (_cmd_resume,  "resume <goal_id>"),
        "delete":  (_cmd_delete,  "delete <goal_id>"),
    }
    if cmd in arg1_cmds:
        fn, usage = arg1_cmds[cmd]
        if len(parts) < 2:
            print(f"用法：{usage}")
            return _cmd_result(cmd, executed=False)
        fn(parts[1])
        return _cmd_result(cmd)

    # runs 命令（参数可选）
    if cmd == "runs":
        limit = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 10
        _cmd_runs(limit)
        return _cmd_result(cmd)

    # 需要子命令解析的命令
    if cmd == "init":
        return _handle_init(user_input, cmd)
    if cmd == "add":
        return _handle_add(user_input, cmd)
    if cmd == "rerun":
        return _handle_rerun(user_input, cmd)
    if cmd == "browser":
        return _handle_browser(user_input, cmd)

    # 退出
    if cmd in ("exit", "quit"):
        print("再见！")
        scheduler.stop()
        sys.exit(0)

    # 自然语言 → agentic loop
    return _chat_harness.run(user_input, goals=goals_mod.list_all(), loops=discover())


_MAX_ASSISTANT_RESPONSE = 400


def _record_conversation(user_input: str, assistant_response: str, interaction: dict) -> None:
    response_text = " ".join(assistant_response.strip().split())
    if len(response_text) > _MAX_ASSISTANT_RESPONSE:
        response_text = response_text[:_MAX_ASSISTANT_RESPONSE - 3] + "..."
    if not user_input.strip():
        return
    try:
        record = _conversation_recorder.build_record(
            user_input=user_input,
            assistant_response=response_text,
            route=interaction.get("route", "unknown"),
            command=interaction.get("command"),
            ai_result=interaction.get("ai_result"),
            execution=interaction.get("execution") or {},
        )
        _conversation_recorder.save(record)
    except Exception as e:
        log.error(f"conversation_records 保存失败: {e}")


def _handle_input(user_input: str) -> None:
    buffer = io.StringIO()
    interaction: dict = {}
    tee = _TeeStdout(sys.stdout, buffer)
    with redirect_stdout(tee):
        interaction = _process_input(user_input)
    _record_conversation(user_input, buffer.getvalue(), interaction)


def _read_user_input() -> str:
    if pt_prompt is not None:
        return pt_prompt(
            "> ",
            mouse_support=False,
            completer=AssistantCompleter(),
            complete_while_typing=True,
            auto_suggest=AutoSuggestFromHistory() if AutoSuggestFromHistory else None,
            history=FileHistory(str(_CLI_HISTORY)) if FileHistory else None,
        ).strip()
    return input("\n> ").strip()


def main() -> None:
    _print_startup_banner()
    scheduler.start()

    try:
        while True:
            try:
                user_input = _read_user_input()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break
            if user_input:
                _handle_input(user_input)
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
