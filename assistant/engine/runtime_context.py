"""
runtime_context — 统一装配运行级上下文。

目标：
- 把 memory / docs / recent records / tools 的装配从各 runtime 中抽离
- 明确哪些数据属于 run 级稳定上下文，哪些数据属于循环内动态状态
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from engine.capability_catalog import CapabilityCatalog, build_capability_catalog
from engine.context import RunContext, ToolRegistry
from engine.memory import MemoryStore
from engine.records import RunRecorder

if TYPE_CHECKING:
    from conversation_records import ConversationRecorder

log = logging.getLogger(__name__)

RECENT_RUNS_LIMIT = 5

_ASSISTANT_DIR = Path(__file__).resolve().parent.parent


@dataclass
class ChatRuntimeContext:
    """CLI chat 的运行级上下文，在一次聊天 run 内固定不变。"""

    user_input: str
    goals: list[dict[str, Any]] = field(default_factory=list)
    loops: dict[str, Any] = field(default_factory=dict)
    recent_conversations: list[dict[str, Any]] = field(default_factory=list)
    user_memory: dict[str, Any] = field(default_factory=dict)
    runtime_doc: str = ""
    capability_catalog: CapabilityCatalog = field(default_factory=CapabilityCatalog)


def assistant_dir() -> Path:
    return _ASSISTANT_DIR


def read_optional_doc(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except Exception as e:
        log.warning(f"[runtime_context] 文档加载失败 path={path.name}: {e}")
        return ""


def load_runtime_doc() -> str:
    return read_optional_doc(_ASSISTANT_DIR / "RUNTIME.md")


def load_loop_doc(loop_name: str) -> str:
    return read_optional_doc(_ASSISTANT_DIR / "loops" / f"{loop_name}.md")


def build_recent_runs(
    recorder: RunRecorder,
    *,
    goal_id: str,
    loop_name: str,
    limit: int = RECENT_RUNS_LIMIT,
) -> dict[str, list[dict[str, Any]]]:
    return {
        "goal_recent_runs": recorder.list_recent_by_goal(goal_id, limit=limit),
        "loop_recent_runs": recorder.list_recent_by_loop(loop_name, limit=limit),
    }


def build_loop_run_context(
    *,
    memory_store: MemoryStore,
    recorder: RunRecorder,
    loop,
    goal: dict[str, Any],
    run_id: str,
    recent_runs_limit: int = RECENT_RUNS_LIMIT,
) -> RunContext:
    """
    构建 loop 的 RunContext。

    这是 run 级装配逻辑：每次 loop run 只做一次，不进入 agentic while-loop。
    """

    loop_name = loop.name
    return RunContext(
        run_id=run_id,
        goal=goal,
        memory=memory_store.load_loop_memory(loop_name),
        goal_memory=memory_store.load_goal_memory(goal["id"]),
        recent_runs=build_recent_runs(
            recorder,
            goal_id=goal["id"],
            loop_name=loop_name,
            limit=recent_runs_limit,
        ),
        runtime_doc=load_runtime_doc(),
        loop_doc=load_loop_doc(loop_name),
        tools=ToolRegistry.build(getattr(loop, "required_tools", [])),
    )


def build_chat_runtime_context(
    *,
    memory_store: MemoryStore,
    conversation_recorder: "ConversationRecorder | None",
    user_input: str,
    goals: list[dict[str, Any]],
    loops: dict[str, Any],
    include_user_memory: bool = True,
    include_recent_conversations: bool = True,
    include_runtime_doc: bool = True,
) -> ChatRuntimeContext:
    """构建 CLI chat 的稳定上下文，不包含循环内 messages。"""

    recent_conversations: list[dict[str, Any]] = []
    if include_recent_conversations and conversation_recorder:
        recent_conversations = conversation_recorder.list_recent(limit=10)

    return ChatRuntimeContext(
        user_input=user_input,
        goals=goals,
        loops=loops,
        recent_conversations=recent_conversations,
        user_memory=memory_store.load_user_memory() if include_user_memory else {},
        runtime_doc=load_runtime_doc() if include_runtime_doc else "",
        capability_catalog=build_capability_catalog(loops),
    )
