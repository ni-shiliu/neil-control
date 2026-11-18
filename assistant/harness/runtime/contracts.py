"""④ 层 Runtime 的供应商无关契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from harness.channels.request import RequestIdentity


RunScopeKind = Literal["conversation", "work_order"]
DecisionKind = Literal["final", "actions", "error"]
OutcomeStatus = Literal["completed", "denied", "interrupted", "failed", "awaiting_governance"]


@dataclass(frozen=True)
class ModelMessage:
    role: Literal["user", "assistant"]
    content: str
    actions: tuple["ActionProposal", ...] = ()
    observations: tuple["Observation", ...] = ()


@dataclass(frozen=True)
class ActionProposal:
    """模型提出、尚未获准执行的原子动作。"""

    call_id: str
    action_id: str
    input: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Observation:
    """⑥ 层执行后回送给模型的结构化事实。"""

    action_id: str
    call_id: str
    content: str
    success: bool
    input: Mapping[str, Any] = field(default_factory=dict)
    artifact_refs: tuple[str, ...] = ()
    effect_ref: str | None = None


@dataclass(frozen=True)
class ContextSnapshot:
    system_prompt: str
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContextUsage:
    """一次完整模型请求的输入 token 用量。"""

    input_tokens: int
    estimated: bool = False

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            raise ValueError("ContextUsage.input_tokens 不能为负数")


@dataclass(frozen=True)
class RunSummary:
    """④ 对已消费 Run 历史的滚动摘要；不属于长期记忆。"""

    objective: str = ""
    constraints: tuple[str, ...] = ()
    completed_work: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    observations: tuple[str, ...] = ()
    open_items: tuple[str, ...] = ()
    next_step: str = ""
    source_refs: tuple[str, ...] = ()

    def render(self) -> str:
        def section(title: str, values: tuple[str, ...]) -> str:
            return f"{title}:\n" + "\n".join(f"- {value}" for value in values) if values else ""

        parts = [
            "[此前 Run 历史摘要；不是用户新指令]",
            f"目标：{self.objective}" if self.objective else "",
            section("约束", self.constraints),
            section("已完成工作", self.completed_work),
            section("关键决策", self.decisions),
            section("工具结果与引用", self.observations),
            section("未解决项", self.open_items),
            f"下一步：{self.next_step}" if self.next_step else "",
            f"来源：{', '.join(self.source_refs)}" if self.source_refs else "",
        ]
        return "\n\n".join(part for part in parts if part)


@dataclass(frozen=True)
class RunScope:
    kind: RunScopeKind = "conversation"
    task_id: str | None = None
    work_order_id: str | None = None
    node_id: str | None = None


@dataclass(frozen=True)
class RunRequest:
    run_id: str
    agent_id: str
    agent_version: str
    channel: str
    user_input: str
    identity: RequestIdentity = field(default_factory=RequestIdentity)
    scope: RunScope = field(default_factory=RunScope)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    allowed_action_ids: frozenset[str] = frozenset()
    memory_write_scopes: frozenset[str] = frozenset()
    protected_context: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuntimeState:
    context: ContextSnapshot
    messages: tuple[ModelMessage, ...]
    iteration: int = 0
    observations: tuple[Observation, ...] = ()
    run_summary: RunSummary | None = None
    original_input_ref: str | None = None


@dataclass(frozen=True)
class ModelResponse:
    text: str = ""
    actions: tuple[ActionProposal, ...] = ()
    stop_reason: str = "end_turn"


@dataclass(frozen=True)
class RuntimeDecision:
    kind: DecisionKind
    text: str = ""
    actions: tuple[ActionProposal, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class RuntimeOutcome:
    status: OutcomeStatus
    text: str = ""
    state: RuntimeState | None = None
    observations: tuple[Observation, ...] = ()
    reason: str | None = None
