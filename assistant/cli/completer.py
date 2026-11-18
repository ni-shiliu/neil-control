"""命令行 Tab 补全器。

命令名从 dispatch.COMMAND_NAMES 派生（单一来源）；
goal_id / loop_name 等动态补全在运行时从 goals / loops 读取。
"""

from __future__ import annotations

import goals as goals_mod
from loops import discover

from cli.dispatch import COMMAND_NAMES, GOAL_ARG_COMMANDS, LOOP_ARG_COMMANDS, _COMMAND_BY_NAME

try:
    from prompt_toolkit.completion import Completer, Completion
except Exception:  # pragma: no cover - 最小环境降级
    Completer = object
    Completion = None


def _current_goal_ids() -> list[str]:
    return [goal.get("id", "") for goal in goals_mod.list_all() if goal.get("id")]


def _current_loop_names() -> list[str]:
    return sorted(discover().keys())


class AssistantCompleter(Completer):

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        stripped = text.lstrip()
        parts = stripped.split()
        word = document.get_word_before_cursor(WORD=True)

        # 首词：补全命令名
        if not parts:
            for cmd in COMMAND_NAMES:
                if cmd.startswith(word):
                    yield Completion(cmd, start_position=-len(word))
            return

        if len(parts) == 1 and not stripped.endswith(" "):
            for cmd in COMMAND_NAMES:
                if cmd.startswith(parts[0]):
                    yield Completion(cmd, start_position=-len(word))
            return

        cmd = parts[0].lower()

        # init：子命令 + 选项；loop 后接 loop 名
        if cmd == "init":
            choices = ["loop", "loops", "--force"]
            if len(parts) >= 2 and parts[1] == "loop":
                choices = _current_loop_names() + ["--with-doc", "--force"]
            for item in choices:
                if item.startswith(word):
                    yield Completion(item, start_position=-len(word))
            return

        # 需要 goal_id 的命令
        if cmd in GOAL_ARG_COMMANDS:
            for goal_id in _current_goal_ids():
                if goal_id.startswith(word):
                    yield Completion(goal_id, start_position=-len(word))
            if cmd == "rerun" and "--dry-run".startswith(word):
                yield Completion("--dry-run", start_position=-len(word))
            return

        # 需要 loop_name 的命令
        if cmd in LOOP_ARG_COMMANDS:
            for loop_name in _current_loop_names():
                if loop_name.startswith(word):
                    yield Completion(loop_name, start_position=-len(word))
            return

        # 其余命令的静态补全项（add / browser 等）取自命令表
        command = _COMMAND_BY_NAME.get(cmd)
        if command and command.completions:
            for item in command.completions:
                if item.startswith(word):
                    yield Completion(item, start_position=-len(word))
            return
