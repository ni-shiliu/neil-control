"""
命令行入口。

自然语言输入 → Claude 解析为 goal（schedule + loop 类型）→ 存储 + 注册调度
管理命令：list / pause <id> / resume <id> / delete <id> / help
"""

import json
import logging
import shlex
import sys
from pathlib import Path

from dotenv import load_dotenv
from claude_client import get_client, get_model
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
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("anthropic").setLevel(logging.WARNING)
logging.getLogger("apscheduler").setLevel(logging.WARNING)
_recorder = RunRecorder()
_memory = MemoryStore()
_ASSISTANT_DIR = Path(__file__).parent
_LOOPS_DIR = _ASSISTANT_DIR / "loops"
_TESTS_DIR = _ASSISTANT_DIR / "tests"
_RUNTIME_DOC = _ASSISTANT_DIR / "RUNTIME.md"
_CLI_HISTORY = _ASSISTANT_DIR / ".cli_history"
_BANNER_FILE = _ASSISTANT_DIR / "BANNER.txt"


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


def _normalize_text(text: str) -> str:
    return "".join(ch.lower() for ch in text.strip() if not ch.isspace())


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
            "add", "help", "exit", "quit",
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

def _call_claude(prompt: str, max_tokens: int = 512) -> str:
    msg = get_client().messages.create(
        model=get_model(),
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _parse_goal(user_input: str) -> dict | None:
    """让 Claude 把自然语言解析成结构化 goal，失败返回 None。"""
    loops = discover()
    loop_lines = "\n".join(f"- {n}：{l.description}" for n, l in loops.items())
    prompt = f"""用户说："{user_input}"

请判断这是否是一个需要执行的任务目标，并解析成结构化数据。

支持的 loop 类型（从下列中选最匹配的一个，或判断为非任务目标）：
{loop_lines}

触发模式说明：
- cron  : 固定时间触发，需要 schedule（如"每天早上8点"）
- goal  : 目标驱动，持续运行直到目标达成（如"保持收件箱零未读"），可搭配初始 schedule
- event : 事件驱动，实时响应（如"有新邮件时立刻处理"），不需要 schedule

如果是任务目标，输出 JSON：
{{
  "is_goal": true,
  "trigger_mode": "cron 或 goal 或 event",
  "schedule": "5字段cron（仅 cron/goal 模式需要，event 填 null）。例：每天10点='0 10 * * *'",
  "goal_condition": "goal 模式时填达成条件描述，其他填 null",
  "loop": "loop类型",
  "summary": "一句话描述这个目标",
  "dry_run": "true 或 false，只有用户明确要求演练/试运行时才为 true",
  "retry_after_minutes": "整数，可选；仅当用户明确要求时返回，否则返回 null",
  "max_retries": "整数，可选；仅当用户明确要求时返回，否则返回 null",
  "retry_backoff_factor": "整数，可选；仅当用户明确要求时返回，否则返回 null",
  "retry_max_minutes": "整数，可选；仅当用户明确要求时返回，否则返回 null"
}}

如果不是任务目标（比如闲聊、问问题），输出：
{{"is_goal": false, "reply": "你的回复"}}

只输出 JSON，不要其他内容。"""

    msg = get_client().messages.create(
        model=get_model(),
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
    return json.loads(raw)


def _cmd_list() -> None:
    goals = goals_mod.list_all()
    if not goals:
        print("暂无目标。")
        return
    print(f"\n{'ID':<15} {'状态':<8} {'Loop':<20} {'Cron':<15} {'原始描述'}")
    print("-" * 90)
    for g in goals:
        status_icon = "✓" if g["status"] == "active" else "⏸"
        schedule_label = g.get("schedule") or "-"
        print(f"{g['id']:<15} {status_icon} {g['status']:<6} {g['loop']:<20} {schedule_label:<15} {g['raw']}")
        if g["last_run"]:
            print(f"  └─ 上次执行: {g['last_run']}  结果: {g['last_result']}")
    print()


def _cmd_list_active_goals() -> None:
    goals = [goal for goal in goals_mod.list_all() if goal.get("status") == "active"]
    if not goals:
        print("当前没有正在运行的 goal。")
        return

    print("当前正在运行的 goal：")
    for idx, goal in enumerate(goals, start=1):
        schedule_label = goal.get("schedule") or "-"
        print(
            f"{idx}. {goal['id']} | loop={goal['loop']} | "
            f"schedule={schedule_label} | {goal['raw']}"
        )
    print()


def _cmd_runs(limit: int = 10) -> None:
    records = _recorder.list_recent(limit=limit)
    if not records:
        print("暂无运行记录。")
        return

    print(f"\n{'Run ID':<14} {'Loop':<22} {'状态':<10} {'DryRun':<8} {'开始时间'}")
    print("-" * 90)
    for r in records:
        print(
            f"{r.get('run_id', ''):<14} "
            f"{r.get('loop_name', ''):<22} "
            f"{r.get('status', ''):<10} "
            f"{str(r.get('dry_run', False)):<8} "
            f"{r.get('started_at', '')}"
        )
        print(f"  └─ {r.get('summary', '')}")
    print()


def _cmd_goal(goal_id: str) -> None:
    goal = goals_mod.get(goal_id)
    if not goal:
        print(f"未找到目标 {goal_id}")
        return
    print(json.dumps(goal, ensure_ascii=False, indent=2))


def _cmd_run(run_id: str) -> None:
    record = _recorder.find_run(run_id)
    if not record:
        print(f"未找到运行记录 {run_id}")
        return

    print(f"\nRun ID:      {record.get('run_id', '')}")
    print(f"Goal ID:     {record.get('goal_id', '')}")
    print(f"Loop:        {record.get('loop_name', '')}")
    print(f"状态:        {record.get('status', '')}")
    print(f"Dry Run:     {record.get('dry_run', False)}")
    print(f"开始:        {record.get('started_at', '')}")
    print(f"结束:        {record.get('ended_at', '')}")
    print(f"耗时(ms):    {record.get('duration_ms', '')}")
    print(f"Summary:     {record.get('summary', '')}")
    print(f"Outputs:     {_result_output_count(record)}")
    print(f"Effects:     {len(record.get('planned_effects', []))} planned / {len(record.get('committed_effects', []))} recorded")
    print(f"Effect状态:  {_format_effect_statuses(record.get('committed_effects', []))}")
    print(f"通知:        {len(record.get('notifications', []))}")
    if record.get("error"):
        print(f"错误:        {record['error']}")
    print()


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


def _cmd_rerun(goal_id: str, *, dry_run_override: bool | None = None) -> None:
    goal = goals_mod.get(goal_id)
    if not goal:
        print(f"未找到目标 {goal_id}")
        return

    dry_run_label = (
        dry_run_override if dry_run_override is not None else goal.get("dry_run", False)
    )
    print(f"立即执行 {goal_id} ... (dry_run={dry_run_label})")
    run_result = scheduler.run_goal_now(goal_id, dry_run_override=dry_run_override)
    if run_result is None:
        print("执行失败，未返回运行结果。")
        return

    print(f"完成: {run_result.summary}")
    print(f"状态: {'success' if run_result.success else 'failed'}")
    print(f"Run ID: {run_result.record.run_id}")
    print(
        f"Effects: {len(run_result.record.planned_effects)} planned / "
        f"{len(run_result.record.committed_effects)} recorded"
    )
    print(f"Effect状态: {_format_effect_statuses(run_result.record.committed_effects)}")
    print(f"Outputs: {_result_output_count(run_result.record.to_dict())}")
    if run_result.record.error:
        print(f"错误: {run_result.record.error}")
    print()


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


def _handle_local_nl_intent(user_input: str) -> bool:
    normalized = _normalize_text(user_input)

    delete_all_patterns = (
        "删除我所有的goal",
        "删除所有goal",
        "删掉所有goal",
        "清空goal",
        "清空goals",
        "删除全部goal",
        "删除所有目标",
        "删掉所有目标",
        "清空目标",
        "删除全部目标",
        "deleteallgoals",
        "deleteallgoal",
        "removeallgoals",
        "clearallgoals",
    )
    if any(pattern in normalized for pattern in delete_all_patterns):
        _cmd_delete_all_goals()
        return True

    list_active_goal_patterns = (
        "当前正在运行的goal有哪些",
        "当前运行中的goal有哪些",
        "当前有哪些goal",
        "现在有哪些goal",
        "有哪些goal",
        "有哪些goals",
        "当前goal有哪些",
        "当前目标有哪些",
        "当前有哪些目标",
        "现在有哪些目标",
        "正在运行的goal有哪些",
        "正在运行的目标有哪些",
        "运行中的goal有哪些",
        "运行中的目标有哪些",
        "列出所有goal",
        "列出所有目标",
        "查看所有goal",
        "查看所有目标",
    )
    if any(pattern in normalized for pattern in list_active_goal_patterns):
        _cmd_list_active_goals()
        return True

    return False


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
""")


def _handle_input(user_input: str) -> None:
    parts = user_input.strip().split()
    if not parts:
        return

    cmd = parts[0].lower()

    if cmd == "list":
        _cmd_list()
    elif cmd == "init":
        parsed = _parse_init_command(user_input)
        if not parsed:
            return
        mode, options = parsed
        if mode == "root":
            _cmd_init(force=options["force"])
        elif mode == "loop":
            _cmd_init_loop(
                options["loop_name"],
                force=options["force"],
                with_doc=options["with_doc"],
            )
        elif mode == "loops":
            _cmd_init_loops(force=options["force"])
    elif cmd == "goal" and len(parts) >= 2:
        _cmd_goal(parts[1])
    elif cmd == "runs":
        limit = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 10
        _cmd_runs(limit)
    elif cmd == "run" and len(parts) >= 2:
        _cmd_run(parts[1])
    elif cmd == "goalmem" and len(parts) >= 2:
        _cmd_goalmem(parts[1])
    elif cmd == "loopmem" and len(parts) >= 2:
        _cmd_loopmem(parts[1])
    elif cmd == "add":
        parsed = _parse_add_command(user_input)
        if not parsed:
            return
        overrides, description = parsed
        try:
            result = _parse_goal(description)
        except Exception as e:
            print(f"解析失败: {e}")
            return
        if not result.get("is_goal"):
            print(result.get("reply", "我不太理解，请重新描述。"))
            return
        _create_goal_from_result(description, result, overrides=overrides)
    elif cmd == "pause" and len(parts) >= 2:
        _cmd_pause(parts[1])
    elif cmd == "resume" and len(parts) >= 2:
        _cmd_resume(parts[1])
    elif cmd == "delete" and len(parts) >= 2:
        _cmd_delete(parts[1])
    elif cmd == "rerun":
        parsed = _parse_rerun_command(user_input)
        if not parsed:
            return
        options, goal_id = parsed
        _cmd_rerun(goal_id, dry_run_override=options.get("dry_run"))
    elif cmd in ("help", "?"):
        _cmd_help()
    elif cmd in ("exit", "quit"):
        print("再见！")
        scheduler.stop()
        sys.exit(0)
    else:
        if _handle_local_nl_intent(user_input):
            return
        # 自然语言，交给 Claude 解析
        try:
            result = _parse_goal(user_input)
        except Exception as e:
            print(f"解析失败: {e}")
            return

        if not result.get("is_goal"):
            print(result.get("reply", "我不太理解，请重新描述。"))
            return

        _create_goal_from_result(user_input, result)


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
