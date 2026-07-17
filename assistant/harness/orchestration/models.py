"""③层的持久化协作契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from harness.tasks.models import PlanNode, now_iso

WorkOrderRole = Literal["worker", "reviewer", "operator"]
ExecutionNodeStatus = Literal["pending", "running", "succeeded", "failed", "cancelled"]
ExecutionPlanStatus = Literal["running", "paused", "succeeded", "failed", "cancelled", "superseded"]
WorkOrderStatus = Literal["issued", "running", "succeeded", "failed", "paused", "cancelled"]


@dataclass(frozen=True)
class ExecutionNode:
    """ExecutionPlan 中一个 PlanNode 的运行态快照。"""

    node: PlanNode
    status: ExecutionNodeStatus = "pending"
    work_order_ids: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    attempts: int = 0
    error: str | None = None


@dataclass(frozen=True)
class ExecutionPlan:
    """③层针对一个 TaskPlan 版本的可恢复调度状态。"""

    id: str
    task_id: str
    task_plan_version: int
    topology: Literal["single_agent", "multi_agent"]
    nodes: tuple[ExecutionNode, ...]
    status: ExecutionPlanStatus = "running"
    supersedes_id: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.id or not self.task_id or self.task_plan_version < 1:
            raise ValueError("ExecutionPlan 缺少有效 id、task_id 或 TaskPlan 版本")
        ids = [node.node.id for node in self.nodes]
        if not ids or len(ids) != len(set(ids)):
            raise ValueError("ExecutionPlan 必须包含唯一节点快照")


@dataclass(frozen=True)
class WorkOrder:
    """协调者发给一个角色明确 Agent 的最小、类型化委派。"""

    id: str
    execution_plan_id: str
    task_id: str
    task_plan_version: int
    node: PlanNode
    role: WorkOrderRole
    agent_id: str
    agent_version: str
    task_objective: str
    task_constraints: tuple[str, ...]
    input_artifact_refs: tuple[str, ...] = ()
    supplemental_input: str = ""
    attempt: int = 1
    status: WorkOrderStatus = "issued"
    run_id: str | None = None
    error: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.id or not self.execution_plan_id or not self.task_id:
            raise ValueError("WorkOrder 缺少 id、execution_plan_id 或 task_id")
        if self.attempt < 1 or not self.agent_id or not self.agent_version:
            raise ValueError("WorkOrder 必须指定有效目标 Agent 与尝试次数")
        if not self.task_objective.strip():
            raise ValueError("WorkOrder 必须包含 Task objective")


@dataclass(frozen=True)
class WorkOrderResult:
    """④层返回给③层的纯结果；③层决定如何沉淀为 Run/Artifact。"""

    success: bool
    text: str = ""
    error: str | None = None

    def __post_init__(self) -> None:
        if self.success and not self.text.strip():
            raise ValueError("成功的 WorkOrderResult 必须包含 Artifact 文本")
        if not self.success and not (self.error or self.text):
            raise ValueError("失败的 WorkOrderResult 必须包含错误原因")


@dataclass(frozen=True)
class ReviewVerdict:
    """Reviewer 对他人 Artifact 的结构化结论；禁止自审由服务校验。"""

    id: str
    task_id: str
    work_order_id: str
    reviewer_agent_id: str
    artifact_refs: tuple[str, ...]
    verdict: Literal["approved", "rejected"]
    evidence: tuple[str, ...]
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.id or not self.task_id or not self.work_order_id or not self.reviewer_agent_id:
            raise ValueError("ReviewVerdict 缺少必要关联")
        if not self.artifact_refs or not self.evidence:
            raise ValueError("ReviewVerdict 必须包含 Artifact 与证据")


@dataclass(frozen=True)
class EffectIntent:
    """Operator 未来消费的外部副作用意图；本期不提交 Effect。"""

    id: str
    task_id: str
    work_order_id: str
    effect_kind: str
    payload: dict[str, Any]
    idempotency_key: str
    status: Literal["proposed", "approved", "rejected", "submitted"] = "proposed"
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.id or not self.task_id or not self.work_order_id or not self.effect_kind:
            raise ValueError("EffectIntent 缺少必要关联")
        if not self.idempotency_key.strip():
            raise ValueError("EffectIntent 必须提供稳定 idempotency_key")


def execution_plan_from_dict(value: dict[str, Any]) -> ExecutionPlan:
    nodes = tuple(ExecutionNode(
        node=PlanNode(
            id=item["node"]["id"], title=item["node"]["title"], description=item["node"]["description"],
            depends_on=tuple(item["node"].get("depends_on", ())),
            acceptance_criteria=tuple(item["node"].get("acceptance_criteria", ())),
        ),
        status=item.get("status", "pending"),
        work_order_ids=tuple(item.get("work_order_ids", ())),
        artifact_refs=tuple(item.get("artifact_refs", ())),
        attempts=int(item.get("attempts", 0)),
        error=item.get("error"),
    ) for item in value["nodes"])
    return ExecutionPlan(
        id=value["id"], task_id=value["task_id"], task_plan_version=int(value["task_plan_version"]),
        topology=value["topology"], nodes=nodes, status=value.get("status", "running"),
        supersedes_id=value.get("supersedes_id"), created_at=value.get("created_at", now_iso()),
        updated_at=value.get("updated_at", now_iso()),
    )


def work_order_from_dict(value: dict[str, Any]) -> WorkOrder:
    node = value["node"]
    return WorkOrder(
        id=value["id"], execution_plan_id=value["execution_plan_id"], task_id=value["task_id"],
        task_plan_version=int(value["task_plan_version"]),
        node=PlanNode(
            id=node["id"], title=node["title"], description=node["description"],
            depends_on=tuple(node.get("depends_on", ())),
            acceptance_criteria=tuple(node.get("acceptance_criteria", ())),
        ),
        role=value["role"], agent_id=value["agent_id"], agent_version=value["agent_version"],
        task_objective=value["task_objective"], task_constraints=tuple(value.get("task_constraints", ())),
        input_artifact_refs=tuple(value.get("input_artifact_refs", ())),
        supplemental_input=value.get("supplemental_input", ""), attempt=int(value.get("attempt", 1)),
        status=value.get("status", "issued"), run_id=value.get("run_id"), error=value.get("error"),
        created_at=value.get("created_at", now_iso()), updated_at=value.get("updated_at", now_iso()),
    )


def review_verdict_from_dict(value: dict[str, Any]) -> ReviewVerdict:
    return ReviewVerdict(
        id=value["id"], task_id=value["task_id"], work_order_id=value["work_order_id"],
        reviewer_agent_id=value["reviewer_agent_id"], artifact_refs=tuple(value["artifact_refs"]),
        verdict=value["verdict"], evidence=tuple(value["evidence"]),
        created_at=value.get("created_at", now_iso()),
    )


def effect_intent_from_dict(value: dict[str, Any]) -> EffectIntent:
    return EffectIntent(
        id=value["id"], task_id=value["task_id"], work_order_id=value["work_order_id"],
        effect_kind=value["effect_kind"], payload=dict(value["payload"]),
        idempotency_key=value["idempotency_key"], status=value.get("status", "proposed"),
        created_at=value.get("created_at", now_iso()),
    )


def as_jsonable(value: Any) -> dict[str, Any]:
    return asdict(value)
