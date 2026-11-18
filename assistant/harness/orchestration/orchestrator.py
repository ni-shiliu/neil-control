"""③层单 Agent 编排器：DAG 调度、WorkOrder、汇总与恢复。"""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from harness.agents.definition import AgentDefinition
from harness.agents.registry import AgentRegistry
from harness.channels import IncomingRequest
from harness.interaction import ExecutionState, Interaction
from harness.orchestration.models import (
    EffectIntent, ExecutionNode, ExecutionPlan, ReviewVerdict, WorkOrder, WorkOrderResult,
)
from harness.orchestration.repository import OrchestrationRepository
from harness.orchestration.selection import AgentSelector, CollaborationSelectionError, TopologySelector
from harness.tasks.models import Artifact, Task, TaskPlan, new_id, now_iso
from harness.tasks.service import TaskService
from harness.tasks.session import TaskContext


class WorkOrderExecutor(Protocol):
    def execute_work_order(
        self,
        request: IncomingRequest,
        *,
        agent: AgentDefinition,
        work_order: WorkOrder,
        input_artifacts: tuple[Artifact, ...],
    ) -> WorkOrderResult: ...


class TaskOrchestrator:
    """③层唯一执行入口；自身不调用模型、不执行工具、不直接写 L2 事实。"""

    def __init__(
        self,
        *,
        task_service: TaskService,
        registry: AgentRegistry,
        executor: WorkOrderExecutor,
        repository: OrchestrationRepository | None = None,
        topology_selector: TopologySelector | None = None,
    ):
        self._tasks = task_service
        self._repository = repository or OrchestrationRepository(task_service.repository)
        self._executor = executor
        self._topology = topology_selector or TopologySelector()
        self._agents = AgentSelector(registry)

    def orchestrate_or_resume(
        self,
        *,
        context: TaskContext,
        request: IncomingRequest,
    ) -> Interaction | None:
        """推进 active Task；没有待执行 Task 时返回 None 交由普通聊天处理。"""
        if not context.has_active_task or context.task is None or context.plan is None:
            return None
        if context.task.status in {"completed", "failed", "cancelled"}:
            return None

        task = context.task
        plan = context.plan
        execution = self._ensure_execution_plan(task, plan)
        execution = self._recover_interrupted(task, execution)
        # _recover_interrupted 可能已把持久 Task 置为 paused；重新读取后才能走
        # resume_task()，避免本次恢复绕过状态迁移与 checkpoint。
        task = self._tasks.repository.get_task(task.id)
        if task.status == "paused":
            task = self._tasks.resume_task(task.id)
            # 首期无工具 Worker 的失败可由后续自然语言输入安全地重新尝试；
            # 保留失败 WorkOrder 历史，并把节点重新放回 ready 队列。
            execution = replace(
                execution,
                nodes=tuple(
                    replace(node, status="pending", error=None) if node.status == "failed" else node
                    for node in execution.nodes
                ),
                status="running",
                updated_at=now_iso(),
            )
            self._repository.save_execution_plan(execution)
            self._tasks.record_checkpoint(
                task.id, kind="execution_resumed", plan_version=plan.version,
                metadata={"execution_plan_id": execution.id},
            )
        return self._run_ready_nodes(task=task, task_plan=plan, execution=execution, request=request)

    def record_review_verdict(self, verdict: ReviewVerdict) -> None:
        """持久化 Reviewer 结论，并强制 Reviewer 不得审查自己的产物。"""
        order = self._repository.get_work_order(verdict.task_id, verdict.work_order_id)
        if order.agent_id == verdict.reviewer_agent_id:
            raise ValueError("Reviewer 不能审批自己创建的 Artifact")
        if not set(verdict.artifact_refs) <= set(self._node_for_order(order).artifact_refs):
            raise ValueError("ReviewVerdict 引用了不属于该 WorkOrder 节点的 Artifact")
        self._repository.save_review_verdict(verdict)
        self._tasks.record_checkpoint(
            verdict.task_id, kind="review_verdict_recorded", plan_version=order.task_plan_version,
            run_id=order.run_id, artifact_refs=verdict.artifact_refs,
            metadata={"work_order_id": order.id, "verdict": verdict.verdict},
        )

    def record_effect_intent(self, intent: EffectIntent) -> None:
        """保存 Effect 意图而不提交外部副作用；Operator/L5/L6 后续消费。"""
        order = self._repository.get_work_order(intent.task_id, intent.work_order_id)
        self._repository.save_effect_intent(intent)
        self._tasks.record_checkpoint(
            intent.task_id, kind="effect_intent_recorded", plan_version=order.task_plan_version,
            run_id=order.run_id,
            metadata={"work_order_id": order.id, "effect_kind": intent.effect_kind},
        )

    def _ensure_execution_plan(self, task: Task, task_plan: TaskPlan) -> ExecutionPlan:
        existing = self._repository.list_execution_plans(task.id)
        current = next(
            (item for item in existing if item.task_plan_version == task_plan.version and item.status != "superseded"),
            None,
        )
        if current is not None:
            return current

        previous = next((item for item in reversed(existing) if item.status != "superseded"), None)
        if previous is not None:
            self._repository.save_execution_plan(replace(previous, status="superseded", updated_at=now_iso()))
        nodes = tuple(self._carry_or_queue(node, previous) for node in task_plan.nodes)
        execution = ExecutionPlan(
            id=new_id("execution_plan"), task_id=task.id, task_plan_version=task_plan.version,
            topology=self._topology.select(task), nodes=nodes,
            supersedes_id=previous.id if previous else None,
        )
        self._repository.save_execution_plan(execution)
        self._tasks.record_checkpoint(
            task.id, kind="execution_plan_changed", plan_version=task_plan.version,
            artifact_refs=tuple(ref for node in nodes for ref in node.artifact_refs),
            metadata={"execution_plan_id": execution.id, "supersedes_id": execution.supersedes_id},
        )
        return execution

    @staticmethod
    def _carry_or_queue(node, previous: ExecutionPlan | None) -> ExecutionNode:
        if previous is not None:
            prior = next((item for item in previous.nodes if item.node == node), None)
            if prior is not None and prior.status == "succeeded":
                return ExecutionNode(
                    node=node, status="succeeded", work_order_ids=prior.work_order_ids,
                    artifact_refs=prior.artifact_refs, attempts=prior.attempts,
                )
        return ExecutionNode(node=node)

    def _recover_interrupted(self, task: Task, execution: ExecutionPlan) -> ExecutionPlan:
        interrupted = [
            order for order in self._repository.list_work_orders(task.id, execution_plan_id=execution.id)
            if order.status == "running"
        ]
        if not interrupted:
            return execution
        for order in interrupted:
            paused = replace(order, status="paused", error="运行中断，等待安全恢复", updated_at=now_iso())
            self._repository.save_work_order(paused)
            if order.run_id:
                try:
                    self._tasks.finish_node_run(task.id, order.run_id, status="paused")
                except ValueError:
                    pass
        nodes = tuple(
            replace(node, status="pending", error="运行中断，下一次输入将重新尝试")
            if any(order.node.id == node.node.id for order in interrupted) else node
            for node in execution.nodes
        )
        recovered = replace(execution, nodes=nodes, status="paused", updated_at=now_iso())
        self._repository.save_execution_plan(recovered)
        if task.status == "in_progress":
            self._tasks.transition_task(task.id, status="paused")
        self._tasks.record_checkpoint(
            task.id, kind="execution_paused", plan_version=execution.task_plan_version,
            metadata={"execution_plan_id": execution.id, "reason": "interrupted"},
        )
        return recovered

    def _run_ready_nodes(
        self,
        *,
        task: Task,
        task_plan: TaskPlan,
        execution: ExecutionPlan,
        request: IncomingRequest,
    ) -> Interaction:
        current = execution
        while True:
            ready = self._next_ready_node(current)
            if ready is None:
                if all(item.status == "succeeded" for item in current.nodes):
                    return self._complete_task(task, current)
                return self._pause(task, current, "计划节点没有可安全执行的路径")
            try:
                agent = self._agents.select_worker(task)
            except CollaborationSelectionError as exc:
                return self._pause(task, current, str(exc))
            current, failure = self._dispatch_and_execute(
                task=task, execution=current, node=ready, request=request, agent=agent,
            )
            if failure is not None:
                return failure

    @staticmethod
    def _next_ready_node(execution: ExecutionPlan) -> ExecutionNode | None:
        by_id = {item.node.id: item for item in execution.nodes}
        for item in execution.nodes:  # TaskPlan 原顺序即首期稳定调度顺序。
            if item.status != "pending":
                continue
            if all(by_id[parent].status == "succeeded" for parent in item.node.depends_on):
                return item
        return None

    def _dispatch_and_execute(
        self,
        *,
        task: Task,
        execution: ExecutionPlan,
        node: ExecutionNode,
        request: IncomingRequest,
        agent: AgentDefinition,
    ) -> tuple[ExecutionPlan, Interaction | None]:
        inputs = self._input_artifacts(task.id, execution, node)
        order = WorkOrder(
            id=new_id("work_order"), execution_plan_id=execution.id, task_id=task.id,
            task_plan_version=execution.task_plan_version, node=node.node, role="worker",
            agent_id=agent.id, agent_version=agent.version, task_objective=task.objective,
            task_constraints=task.constraints, input_artifact_refs=tuple(self._artifact_ref(item) for item in inputs),
            supplemental_input=request.raw_text, attempt=node.attempts + 1,
        )
        self._repository.save_work_order(order)
        issued = self._replace_node(
            execution, node.node.id,
            status="running", work_order_ids=node.work_order_ids + (order.id,), attempts=order.attempt,
            error=None,
        )
        self._repository.save_execution_plan(issued)
        self._tasks.record_checkpoint(
            task.id, kind="work_order_issued", plan_version=issued.task_plan_version,
            metadata={"execution_plan_id": issued.id, "work_order_id": order.id, "node_id": node.node.id},
        )

        run = self._tasks.start_node_run(task.id, node_id=node.node.id, retryable=True)
        running_order = replace(order, status="running", run_id=run.id, updated_at=now_iso())
        self._repository.save_work_order(running_order)
        try:
            result = self._executor.execute_work_order(
                request, agent=agent, work_order=running_order, input_artifacts=inputs,
            )
        except Exception as exc:  # Runtime 异常也必须被收口为可恢复节点失败。
            result = WorkOrderResult(success=False, error=str(exc))

        if not result.success:
            self._tasks.finish_node_run(task.id, run.id, status="failed")
            failed_order = replace(running_order, status="failed", error=result.error or result.text, updated_at=now_iso())
            self._repository.save_work_order(failed_order)
            failed = replace(self._replace_node(issued, node.node.id, status="failed", error=failed_order.error), status="paused", updated_at=now_iso())
            self._repository.save_execution_plan(failed)
            return failed, self._pause(task, failed, failed_order.error or "节点执行失败")

        artifact = Artifact(
            id=new_id("artifact"), task_id=task.id, run_id=run.id, kind="work_order_result",
            media_type="application/json", content={
                "work_order_id": running_order.id, "node_id": node.node.id,
                "agent_id": agent.id, "text": result.text,
            },
        )
        self._tasks.save_artifact(artifact)
        artifact_ref = self._artifact_ref(artifact)
        self._tasks.finish_node_run(task.id, run.id, status="succeeded", artifact_refs=(artifact_ref,))
        self._repository.save_work_order(replace(running_order, status="succeeded", updated_at=now_iso()))
        succeeded = self._replace_node(
            issued, node.node.id, status="succeeded", artifact_refs=(artifact_ref,), error=None,
        )
        self._repository.save_execution_plan(succeeded)
        self._tasks.record_checkpoint(
            task.id, kind="work_order_completed", plan_version=succeeded.task_plan_version,
            run_id=run.id, artifact_refs=(artifact_ref,),
            metadata={"execution_plan_id": succeeded.id, "work_order_id": running_order.id},
        )
        return succeeded, None

    def _complete_task(self, task: Task, execution: ExecutionPlan) -> Interaction:
        refs = tuple(ref for node in execution.nodes for ref in node.artifact_refs)
        completed = replace(execution, status="succeeded", updated_at=now_iso())
        self._repository.save_execution_plan(completed)
        self._tasks.transition_task(task.id, status="completed", evidence_refs=refs)
        return Interaction(
            route="orchestrated",
            text=self._summary(task.id, completed),
            execution=ExecutionState(executed=True, kind="orchestration", success=True),
        )

    def _pause(self, task: Task, execution: ExecutionPlan, reason: str) -> Interaction:
        paused = replace(execution, status="paused", updated_at=now_iso())
        self._repository.save_execution_plan(paused)
        if task.status in {"planned", "in_progress"}:
            self._tasks.transition_task(task.id, status="paused")
        self._tasks.record_checkpoint(
            task.id, kind="execution_paused", plan_version=paused.task_plan_version,
            metadata={"execution_plan_id": paused.id, "reason": reason},
        )
        return Interaction(
            route="orchestration_paused", text=f"处理已暂停：{reason}",
            execution=ExecutionState(executed=False, kind="orchestration", success=False, reason="paused"),
        )

    def _input_artifacts(self, task_id: str, execution: ExecutionPlan, node: ExecutionNode) -> tuple[Artifact, ...]:
        by_id = {item.node.id: item for item in execution.nodes}
        artifacts: list[Artifact] = []
        for dependency in node.node.depends_on:
            for ref in by_id[dependency].artifact_refs:
                artifact_id, version = self._split_artifact_ref(ref)
                artifacts.append(self._tasks.repository.get_artifact(task_id, artifact_id, version))
        return tuple(artifacts)

    @staticmethod
    def _artifact_ref(artifact: Artifact) -> str:
        return f"{artifact.id}:v{artifact.version}"

    @staticmethod
    def _split_artifact_ref(ref: str) -> tuple[str, int]:
        artifact_id, separator, version = ref.partition(":v")
        if not separator or not artifact_id or not version.isdigit():
            raise ValueError(f"无效 Artifact 引用: {ref}")
        return artifact_id, int(version)

    @staticmethod
    def _replace_node(execution: ExecutionPlan, node_id: str, **changes) -> ExecutionPlan:
        nodes = tuple(
            replace(item, **changes) if item.node.id == node_id else item
            for item in execution.nodes
        )
        return replace(execution, nodes=nodes, updated_at=now_iso())

    def _summary(self, task_id: str, execution: ExecutionPlan) -> str:
        sections: list[str] = []
        for node in execution.nodes:
            if not node.artifact_refs:
                continue
            artifact_id, version = self._split_artifact_ref(node.artifact_refs[-1])
            artifact = self._tasks.repository.get_artifact(task_id, artifact_id, version)
            text = artifact.content.get("text", "") if isinstance(artifact.content, dict) else str(artifact.content or "")
            if text.strip():
                sections.append(f"{node.node.title}\n{text.strip()}")
        return "\n\n".join(sections) or "已完成。"

    def _node_for_order(self, order: WorkOrder) -> ExecutionNode:
        execution = self._repository.get_execution_plan(order.task_id, order.execution_plan_id)
        for node in execution.nodes:
            if node.node.id == order.node.id:
                return node
        raise ValueError("WorkOrder 引用了不存在的 ExecutionPlan 节点")
