"""第二层 Task / Plan 的持久化领域对象。

这些对象只描述可审计事实；任务执行、节点状态推进与重试由后续层负责。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _required_texts(values: tuple[str, ...], label: str) -> None:
    if not values or any(not value.strip() for value in values):
        raise ValueError(f"{label} 至少需要一条非空内容")


@dataclass(frozen=True)
class PlanNode:
    id: str
    title: str
    description: str
    depends_on: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.title.strip() or not self.description.strip():
            raise ValueError("PlanNode 的 id、title、description 不能为空")
        _required_texts(self.acceptance_criteria, f"节点 {self.id} 的验收条件")


def validate_dag(nodes: tuple[PlanNode, ...]) -> None:
    if not nodes:
        raise ValueError("TaskPlan 至少需要一个节点")
    by_id = {node.id: node for node in nodes}
    if len(by_id) != len(nodes):
        raise ValueError("TaskPlan 存在重复节点 id")
    for node in nodes:
        unknown = set(node.depends_on) - set(by_id)
        if unknown:
            raise ValueError(f"节点 {node.id} 引用了不存在的依赖: {', '.join(sorted(unknown))}")
        if node.id in node.depends_on:
            raise ValueError(f"节点 {node.id} 不能依赖自身")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise ValueError("TaskPlan 依赖图存在环")
        if node_id in visited:
            return
        visiting.add(node_id)
        for parent in by_id[node_id].depends_on:
            visit(parent)
        visiting.remove(node_id)
        visited.add(node_id)

    for node in nodes:
        visit(node.id)


@dataclass(frozen=True)
class TaskPlan:
    task_id: str
    version: int
    nodes: tuple[PlanNode, ...]
    acceptance_criteria: tuple[str, ...]
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("TaskPlan version 必须从 1 开始")
        _required_texts(self.acceptance_criteria, "TaskPlan 的验收条件")
        validate_dag(self.nodes)


@dataclass(frozen=True)
class Task:
    id: str
    title: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    agent_id: str
    agent_version: str
    workflow_id: str
    workflow_version: str
    origin_channel: str
    origin_request_id: str
    constraints: tuple[str, ...] = ()
    owner_id: str = "local"
    tenant_id: str = "local"
    project_id: str | None = None
    status: Literal["planned", "in_progress", "paused", "completed", "failed", "cancelled"] = "planned"
    current_plan_version: int = 1
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.id or not self.title.strip() or not self.objective.strip():
            raise ValueError("Task 的 id、title、objective 不能为空")
        _required_texts(self.acceptance_criteria, "Task 的验收条件")
        if any(not constraint.strip() for constraint in self.constraints):
            raise ValueError("Task constraints 不能包含空内容")
        if self.current_plan_version < 1:
            raise ValueError("Task current_plan_version 必须从 1 开始")


@dataclass(frozen=True)
class TaskProposal:
    title: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    nodes: tuple[PlanNode, ...]
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.title.strip() or not self.objective.strip():
            raise ValueError("TaskProposal 的 title、objective 不能为空")
        _required_texts(self.acceptance_criteria, "TaskProposal 的验收条件")
        if any(not constraint.strip() for constraint in self.constraints):
            raise ValueError("TaskProposal constraints 不能包含空内容")
        validate_dag(self.nodes)


PatchKind = Literal["add_node", "update_node", "remove_node", "set_acceptance_criteria"]


@dataclass(frozen=True)
class PlanPatchOperation:
    kind: PatchKind
    node: PlanNode | None = None
    node_id: str | None = None
    acceptance_criteria: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if self.kind in ("add_node", "update_node") and self.node is None:
            raise ValueError(f"{self.kind} 必须提供 node")
        if self.kind == "remove_node" and not self.node_id:
            raise ValueError("remove_node 必须提供 node_id")
        if self.kind == "set_acceptance_criteria":
            if self.acceptance_criteria is None:
                raise ValueError("set_acceptance_criteria 必须提供验收条件")
            _required_texts(self.acceptance_criteria, "TaskPlan 的验收条件")


@dataclass(frozen=True)
class PlanPatch:
    task_id: str
    base_version: int
    operations: tuple[PlanPatchOperation, ...]

    def __post_init__(self) -> None:
        if self.base_version < 1 or not self.operations:
            raise ValueError("PlanPatch 必须指定有效 base_version 和至少一个操作")


@dataclass(frozen=True)
class Run:
    id: str
    task_id: str
    plan_version: int
    mode: Literal["legacy_whole_task", "node"]
    status: Literal["pending", "running", "succeeded", "failed", "paused", "cancelled"]
    node_id: str | None = None
    retryable: bool = False
    request_id: str | None = None
    started_at: str = field(default_factory=now_iso)
    ended_at: str | None = None

    def __post_init__(self) -> None:
        if self.plan_version < 1:
            raise ValueError("Run 必须关联有效 Plan version")
        if self.mode == "node" and not self.node_id:
            raise ValueError("节点级 Run 必须提供 node_id")
        if self.mode == "legacy_whole_task" and self.retryable:
            raise ValueError("legacy_whole_task Run 不可重试")
        if self.status in {"succeeded", "failed", "paused", "cancelled"} and not self.ended_at:
            raise ValueError("终态 Run 必须记录 ended_at")


@dataclass(frozen=True)
class Artifact:
    id: str
    task_id: str
    run_id: str
    kind: str
    media_type: str
    version: int = 1
    content: str | dict[str, Any] | list[Any] | None = None
    file_ref: str | None = None
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("Artifact version 必须从 1 开始")
        if self.content is None and not self.file_ref:
            raise ValueError("Artifact 必须包含 content 或 file_ref")
        if self.content is not None and self.file_ref is not None:
            raise ValueError("Artifact 不能同时包含 content 与 file_ref")


@dataclass(frozen=True)
class EffectReference:
    id: str
    task_id: str
    run_id: str
    effect_kind: str
    status: str
    idempotency_key: str = ""
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("EffectReference 必须提供稳定的 idempotency_key")


@dataclass(frozen=True)
class Checkpoint:
    id: str
    task_id: str
    kind: str
    plan_version: int | None = None
    run_id: str | None = None
    artifact_refs: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskRequestLink:
    """请求到 Task 的最小关联，不保存完整 IncomingRequest。"""

    task_id: str
    request_id: str
    channel: str
    created_at: str = field(default_factory=now_iso)
    continuation_key: str | None = None

    def __post_init__(self) -> None:
        if not self.task_id or not self.request_id or not self.channel:
            raise ValueError("TaskRequestLink 的 task_id、request_id、channel 不能为空")


def as_jsonable(value: Any) -> dict[str, Any]:
    return asdict(value)
