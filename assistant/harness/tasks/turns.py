"""②层的 Task 回合生命周期编排。"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from harness.agents.definition import AgentDefinition
from harness.channels import IncomingRequest
from harness.interaction import ExecutionState, Interaction
from harness.tasks.intake import TaskIntakeDecision
from harness.tasks.models import Task
from harness.tasks.repository import TaskRepository
from harness.tasks.service import TaskService
from harness.tasks.session import TaskContext, TaskSessionResolver

log = logging.getLogger(__name__)


class TaskIntentAssessor(Protocol):
    """L4 提案回调的最小契约；Task 层不依赖具体 Runtime。"""

    def __call__(
        self,
        request: IncomingRequest,
        *,
        agent: AgentDefinition,
        task_summary: str,
        active_task_id: str | None,
    ) -> TaskIntakeDecision: ...


@dataclass(frozen=True)
class TaskTurn:
    """一个渠道回合在 L2 中确定的执行上下文与记录目标。"""

    context: TaskContext
    record_task: Task | None = None
    terminal_interaction: Interaction | None = None


class TaskTurnService:
    """Task 回合的唯一入口：准备执行，并在执行后记录事实。"""

    def __init__(self, service: TaskService, sessions: TaskSessionResolver):
        self._service = service
        self._sessions = sessions

    @property
    def service(self) -> TaskService:
        """供 Harness 组合 L3 使用；Task 写入边界仍在 TaskService。"""
        return self._service

    @classmethod
    def build_default(cls, *, memory_projector=None) -> "TaskTurnService":
        repository = TaskRepository()
        return cls(TaskService(repository, memory_projector=memory_projector), TaskSessionResolver(repository))

    def prepare_turn(
        self,
        *,
        request: IncomingRequest,
        agent: AgentDefinition,
        assess_task_intent: TaskIntentAssessor,
    ) -> TaskTurn:
        """解析 Task、接受模型意图并准备一次 Runtime 执行。"""
        context = self._sessions.resolve(request)
        # 这是 L2 的当前执行事实（Task/Plan），不是长期记忆域。即使一个
        # Agent 没有 memory.project（例如 CLI chat），它仍需在续接复杂任务时
        # 看见当前 Task 摘要，才能提出正确的 PlanPatch。
        task_summary = context.summary
        decision = assess_task_intent(
            request,
            agent=agent,
            task_summary=task_summary,
            active_task_id=context.task.id if context.task else None,
        )
        return self._accept_intent(request=request, agent=agent, context=context, decision=decision)

    def complete_turn(
        self,
        turn: TaskTurn,
        *,
        request: IncomingRequest,
        interaction: Interaction,
    ) -> Interaction:
        """记录已执行回合的不可重试整体 Run；不改变回复。"""
        if turn.record_task is None:
            return interaction
        try:
            self._service.record_legacy_run(
                turn.record_task,
                request_id=request.request_id,
                interaction=interaction,
            )
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Task 整体 Run 记录失败: %s", exc)
        return interaction

    def _accept_intent(
        self,
        *,
        request: IncomingRequest,
        agent: AgentDefinition,
        context: TaskContext,
        decision: TaskIntakeDecision,
    ) -> TaskTurn:
        if decision.kind == "ordinary":
            return TaskTurn(context=context)
        if decision.kind == "clarify":
            return TaskTurn(
                context=context,
                terminal_interaction=Interaction(
                    route="task_clarify",
                    text=decision.clarification or "请补充任务目标与验收条件。",
                    execution=ExecutionState(executed=False, kind="task_intake"),
                ),
            )
        try:
            if decision.kind == "create_task":
                if decision.proposal is None:
                    raise ValueError("缺少 TaskProposal")
                task = self._service.create_task(decision.proposal, request=request, agent=agent)
                self._sessions.activate(task)
                self._service.record_checkpoint(
                    task.id,
                    kind="model_decision",
                    plan_version=task.current_plan_version,
                    metadata={"decision": "create_task"},
                )
                return TaskTurn(
                    context=self._sessions.resolve(request),
                    record_task=task,
                )
            if decision.kind == "patch_active_task":
                if decision.patch is None:
                    raise ValueError("缺少 PlanPatch")
                plan = self._service.apply_patch(decision.patch, request=request)
                task = self._service.repository.get_task(decision.patch.task_id)
                self._service.record_checkpoint(
                    task.id,
                    kind="model_decision",
                    plan_version=plan.version,
                    metadata={"decision": "patch_active_task", "base_version": decision.patch.base_version},
                )
                return TaskTurn(context=TaskContext(task=task, plan=plan), record_task=task)
            raise ValueError(f"未知 Task 意图: {decision.kind}")
        except (KeyError, ValueError) as exc:
            return TaskTurn(
                context=context,
                terminal_interaction=Interaction(
                    route="task_rejected",
                    text=f"任务计划需要补充后再继续：{exc}",
                    execution=ExecutionState(executed=False, kind="task_intake", reason="rejected"),
                ),
            )
