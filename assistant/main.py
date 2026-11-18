"""命令行入口。

只负责启动：加载配置、配置日志、组装 CliContext、启动调度器与 REPL。
全部命令分发 / handler / 渲染 / 补全逻辑在 cli/ 包，六层内核在 harness/。
"""

import io
import logging
import sys
from contextlib import redirect_stdout
from pathlib import Path

from dotenv import load_dotenv

import scheduler
from cli.completer import AssistantCompleter
from cli.context import CliContext
from cli.dispatch import dispatch
from harness.interaction import Interaction

try:
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.history import FileHistory
except Exception:  # pragma: no cover - fallback for minimal envs
    pt_prompt = None
    AutoSuggestFromHistory = None
    FileHistory = None

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

_ASSISTANT_DIR = Path(__file__).parent
_CLI_HISTORY = _ASSISTANT_DIR / ".cli_history"
_BANNER_FILE = _ASSISTANT_DIR / "BANNER.txt"


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


# ── Banner ────────────────────────────────────────────────────────────────────

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


def _print_startup_banner(ctx: CliContext) -> None:
    import goals as goals_mod
    from loops import discover

    goals = goals_mod.list_all()
    active_goals = sum(1 for goal in goals if goal.get("status") == "active")
    loops = sorted(discover().keys())

    print(_load_banner())
    print("NEIL_ASSISTANT")
    print("  Personal Loop Runtime for Goals, Memory and Automation")
    print(f"  loops: {len(loops)} loaded")
    print(f"  goals: {active_goals} active / {len(goals)} total")
    print(f"  runtime doc: {'enabled' if ctx.runtime_doc.exists() else 'disabled'}")
    print("  help: 输入 help 查看命令\n")


# ── REPL ──────────────────────────────────────────────────────────────────────

def _handle_input(ctx: CliContext, user_input: str) -> bool:
    """处理一条输入。返回 True 表示请求退出 REPL。"""
    buffer = io.StringIO()
    interaction = Interaction(route="unknown")
    tee = _TeeStdout(sys.stdout, buffer)
    try:
        with redirect_stdout(tee):
            interaction = dispatch(ctx, user_input)
    except KeyboardInterrupt:
        print("\n已取消当前操作。")
        return False
    if interaction.text:
        print(interaction.text)
    if interaction.route == "exit":
        print("再见！")
        return True
    return False


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
    ctx = CliContext.build(channel_id="cli", assistant_dir=_ASSISTANT_DIR)
    try:
        ctx.harness.cleanup_retention()
    except OSError as exc:
        log.warning(f"Harness 记忆保留期清理失败: {exc}")
    _print_startup_banner(ctx)
    scheduler.start()

    try:
        while True:
            try:
                user_input = _read_user_input()
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break
            if user_input and _handle_input(ctx, user_input):
                break
    finally:
        scheduler.stop()


if __name__ == "__main__":
    main()
