"""②层的瞬时 Task 会话关联与请求解析。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from harness.channels import IncomingRequest
from harness.tasks.models import Task, TaskPlan
from harness.tasks.repository import TaskRepository

log = logging.getLogger(__name__)


@dataclass
class TaskSession:
    """单个渠道会话的当前 Task；不作为持久事实存储。"""

    current_task_id: str | None = None


@dataclass(frozen=True)
class TaskContext:
    task: Task | None = None
    plan: TaskPlan | None = None

    @property
    def has_active_task(self) -> bool:
        return self.task is not None and self.plan is not None

    @property
    def summary(self) -> str:
        if not self.task or not self.plan:
            return "(none)"
        nodes = "; ".join(f"{node.id}: {node.title}" for node in self.plan.nodes)
        return (
            f"task={self.task.id}; objective={self.task.objective}; "
            f"plan=v{self.plan.version}; nodes={nodes}"
        )


class TaskSessionResolver:
    def __init__(self, repository: TaskRepository, session: TaskSession | None = None):
        self._repository = repository
        self._session = session or TaskSession()

    def resolve(self, request: IncomingRequest) -> TaskContext:
        linked_task_id = request.identity.task_id or request.metadata.get("task_id")
        if isinstance(linked_task_id, str) and linked_task_id:
            try:
                self._repository.get_task(linked_task_id)
            except KeyError:
                log.warning("忽略不存在的 Task 关联: %s", linked_task_id)
            else:
                self._session.current_task_id = linked_task_id
        if not self._session.current_task_id:
            return TaskContext()
        try:
            task = self._repository.get_task(self._session.current_task_id)
            plan = self._repository.get_plan(task.id, task.current_plan_version)
        except KeyError:
            self._session.current_task_id = None
            return TaskContext()
        return TaskContext(task=task, plan=plan)

    def activate(self, task: Task) -> None:
        self._session.current_task_id = task.id
