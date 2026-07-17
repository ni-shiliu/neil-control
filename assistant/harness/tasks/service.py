"""Task/Plan 的唯一写入服务。"""

from __future__ import annotations

from dataclasses import replace
from typing import Mapping, Protocol

from harness.agents.definition import AgentDefinition
from harness.channels import IncomingRequest
from harness.interaction import Interaction
from harness.tasks.models import (
    Artifact, Checkpoint, EffectReference, PlanNode, PlanPatch, Run, Task, TaskPlan,
    TaskProposal, TaskRequestLink, new_id, now_iso,
)
from harness.tasks.repository import TaskRepository


class TaskMemoryProjectorPort(Protocol):
    def plan_changed(self, task: Task, plan: TaskPlan) -> None: ...


class TaskService:
    def __init__(self, repository: TaskRepository, memory_projector: TaskMemoryProjectorPort | None = None):
        self.repository = repository
        self._memory_projector = memory_projector

    def create_task(self, proposal: TaskProposal, *, request: IncomingRequest, agent: AgentDefinition) -> Task:
        task = Task(
            id=new_id("task"), title=proposal.title, objective=proposal.objective,
            acceptance_criteria=proposal.acceptance_criteria,
            agent_id=agent.id, agent_version=agent.version,
            workflow_id=agent.workflow_template.id,
            workflow_version=agent.workflow_template.version,
            origin_channel=request.channel, origin_request_id=request.request_id,
            constraints=proposal.constraints,
            owner_id=request.identity.user_id or "local",
            tenant_id=request.identity.tenant_id or "local",
            project_id=request.identity.project_id,
        )
        plan = TaskPlan(
            task_id=task.id, version=1, nodes=proposal.nodes,
            acceptance_criteria=proposal.acceptance_criteria,
        )
        self.repository.save_task(task)
        self.repository.save_plan(plan)
        self.attach_request(task.id, request)
        self.record_checkpoint(task.id, kind="plan_changed", plan_version=1, metadata={"reason": "created"})
        self._project_plan(task, plan)
        return task

    def attach_request(self, task_id: str, request: IncomingRequest) -> TaskRequestLink:
        self.repository.get_task(task_id)  # 先确认关联目标存在。
        link = TaskRequestLink(
            task_id=task_id, request_id=request.request_id, channel=request.channel,
            continuation_key=str(request.metadata.get("continuation_key"))
            if request.metadata.get("continuation_key") is not None else None,
        )
        self.repository.save_task_request_link(link)
        return link

    def apply_patch(self, patch: PlanPatch, *, request: IncomingRequest | None = None) -> TaskPlan:
        task = self.repository.get_task(patch.task_id)
        if patch.base_version != task.current_plan_version:
            raise ValueError(
                f"PlanPatch 版本过期: 当前为 v{task.current_plan_version}，收到 v{patch.base_version}"
            )
        previous = self.repository.get_plan(task.id, task.current_plan_version)
        nodes = {node.id: node for node in previous.nodes}
        criteria = previous.acceptance_criteria
        for operation in patch.operations:
            if operation.kind == "add_node":
                assert operation.node is not None
                if operation.node.id in nodes:
                    raise ValueError(f"不能新增已存在节点: {operation.node.id}")
                nodes[operation.node.id] = operation.node
            elif operation.kind == "update_node":
                assert operation.node is not None
                if operation.node.id not in nodes:
                    raise ValueError(f"不能更新不存在节点: {operation.node.id}")
                nodes[operation.node.id] = operation.node
            elif operation.kind == "remove_node":
                assert operation.node_id is not None
                if operation.node_id not in nodes:
                    raise ValueError(f"不能删除不存在节点: {operation.node_id}")
                if any(operation.node_id in node.depends_on for node in nodes.values() if node.id != operation.node_id):
                    raise ValueError(f"不能删除仍被依赖的节点: {operation.node_id}")
                del nodes[operation.node_id]
            elif operation.kind == "set_acceptance_criteria":
                assert operation.acceptance_criteria is not None
                criteria = operation.acceptance_criteria

        version = previous.version + 1
        plan = TaskPlan(
            task_id=task.id, version=version, nodes=tuple(nodes.values()),
            acceptance_criteria=criteria,
        )
        updated_task = replace(task, current_plan_version=version, updated_at=now_iso())
        self.repository.save_plan(plan)
        self.repository.save_task(updated_task)
        if request is not None:
            self.attach_request(task.id, request)
        self.record_checkpoint(
            task.id, kind="plan_changed", plan_version=version,
            metadata={"reason": "patch", "base_version": patch.base_version},
        )
        self._project_plan(updated_task, plan)
        return plan

    def _project_plan(self, task: Task, plan: TaskPlan) -> None:
        if self._memory_projector is None:
            return
        try:
            self._memory_projector.plan_changed(task, plan)
        except (OSError, ValueError, TypeError):
            # 记忆是派生数据，不能破坏已经成功的事实写入。
            return

    def start_node_run(self, task_id: str, *, node_id: str, retryable: bool = True) -> Run:
        task = self.repository.get_task(task_id)
        plan = self.repository.get_plan(task.id, task.current_plan_version)
        if node_id not in {node.id for node in plan.nodes}:
            raise ValueError(f"Plan v{plan.version} 不存在节点: {node_id}")
        run = Run(
            id=new_id("run"), task_id=task.id, plan_version=plan.version, mode="node",
            node_id=node_id, status="running", retryable=retryable,
        )
        self.repository.save_run(run)
        if task.status == "planned":
            self.repository.save_task(replace(task, status="in_progress", updated_at=now_iso()))
        self.record_checkpoint(task.id, kind="run_started", plan_version=plan.version, run_id=run.id)
        return run

    def finish_node_run(
        self,
        task_id: str,
        run_id: str,
        *,
        status: str,
        artifact_refs: tuple[str, ...] = (),
    ) -> Run:
        if status not in {"succeeded", "failed", "paused", "cancelled"}:
            raise ValueError(f"无效节点 Run 终态: {status}")
        run = self.repository.get_run(task_id, run_id)
        if run.mode != "node" or run.status not in {"pending", "running"}:
            raise ValueError("只能结束尚未终态的节点级 Run")
        completed = replace(run, status=status, ended_at=now_iso())
        self.repository.save_run(completed)
        self.record_checkpoint(
            task_id, kind="run_terminal", plan_version=run.plan_version, run_id=run.id,
            artifact_refs=artifact_refs, metadata={"status": status},
        )
        return completed

    def transition_task(
        self,
        task_id: str,
        *,
        status: str,
        evidence_refs: tuple[str, ...] = (),
    ) -> Task:
        """推进 Task 生命周期；完成 Task 至少需指向可审计证据。"""
        if status not in {"paused", "completed", "failed", "cancelled"}:
            raise ValueError(f"无效 Task 状态: {status}")
        task = self.repository.get_task(task_id)
        if task.status in {"completed", "failed", "cancelled"}:
            raise ValueError(f"终态 Task 不可再推进: {task.status}")
        if status == "completed" and not evidence_refs:
            raise ValueError("完成 Task 必须提供验收证据引用")
        if any(run.status in {"pending", "running"} for run in self.repository.list_runs(task_id)):
            raise ValueError("仍有运行中的 Run，不能结束 Task")
        updated = replace(task, status=status, updated_at=now_iso())
        self.repository.save_task(updated)
        self.record_checkpoint(
            task_id, kind="task_state_changed", plan_version=task.current_plan_version,
            artifact_refs=evidence_refs, metadata={"status": status},
        )
        return updated

    def resume_task(self, task_id: str) -> Task:
        """恢复暂停的 Task；节点重试由③层另行创建新的 Run。"""
        task = self.repository.get_task(task_id)
        if task.status == "in_progress":
            return task
        if task.status != "paused":
            raise ValueError(f"只能恢复 paused Task，当前状态: {task.status}")
        if any(run.status == "running" for run in self.repository.list_runs(task_id)):
            raise ValueError("仍有运行中的 Run，不能恢复 Task")
        updated = replace(task, status="in_progress", updated_at=now_iso())
        self.repository.save_task(updated)
        self.record_checkpoint(
            task_id, kind="task_state_changed", plan_version=updated.current_plan_version,
            metadata={"status": "in_progress", "reason": "resumed"},
        )
        return updated

    def save_artifact(self, artifact: Artifact) -> None:
        run = self.repository.get_run(artifact.task_id, artifact.run_id)
        if run.task_id != artifact.task_id:
            raise ValueError("Artifact 与 Run 不属于同一 Task")
        existing = self.repository.list_artifact_versions(artifact.task_id, artifact.id)
        expected_version = len(existing) + 1
        if artifact.version != expected_version:
            raise ValueError(f"Artifact {artifact.id} 应写入 v{expected_version}")
        self.repository.save_artifact(artifact)
        self.record_checkpoint(
            artifact.task_id, kind="artifact_committed", plan_version=run.plan_version,
            run_id=run.id, artifact_refs=(f"{artifact.id}:v{artifact.version}",),
        )

    def record_effect_reference(self, effect: EffectReference) -> None:
        run = self.repository.get_run(effect.task_id, effect.run_id)
        self.repository.save_effect_reference(effect)
        self.record_checkpoint(
            effect.task_id, kind="effect_committed", plan_version=run.plan_version,
            run_id=run.id, metadata={"effect_id": effect.id, "idempotency_key": effect.idempotency_key},
        )

    def record_checkpoint(
        self,
        task_id: str,
        *,
        kind: str,
        plan_version: int | None = None,
        run_id: str | None = None,
        artifact_refs: tuple[str, ...] = (),
        metadata: dict | None = None,
    ) -> Checkpoint:
        checkpoint = Checkpoint(
            id=new_id("checkpoint"), task_id=task_id, kind=kind, plan_version=plan_version,
            run_id=run_id, artifact_refs=artifact_refs, metadata=dict(metadata or {}),
        )
        self.repository.save_checkpoint(checkpoint)
        return checkpoint

    def record_legacy_run(
        self,
        task: Task,
        *,
        request_id: str,
        interaction: Interaction | Mapping,
    ) -> Run:
        """记录当前 ChatRuntime 回合，但不把它伪装为可重试的 Plan 节点执行。"""
        interaction = Interaction.coerce(interaction)
        execution = interaction.execution
        run = Run(
            id=new_id("run"), task_id=task.id, plan_version=task.current_plan_version,
            mode="legacy_whole_task", status="succeeded" if execution.success is not False else "failed",
            retryable=False, request_id=request_id, ended_at=now_iso(),
        )
        self.repository.save_run(run)
        response_artifact = Artifact(
            id=new_id("artifact"), task_id=task.id, run_id=run.id,
            kind="chat_response", media_type="text/plain", content=interaction.text,
        )
        tools_artifact = Artifact(
            id=new_id("artifact"), task_id=task.id, run_id=run.id,
            kind="tool_calls", media_type="application/json",
            content=[call.to_dict() for call in interaction.tool_calls],
        )
        self.save_artifact(response_artifact)
        self.save_artifact(tools_artifact)
        self.record_checkpoint(
            task.id, kind="run_terminal", plan_version=run.plan_version, run_id=run.id,
            artifact_refs=(f"{response_artifact.id}:v1", f"{tools_artifact.id}:v1"),
            metadata={"status": run.status, "mode": run.mode},
        )
        return run
