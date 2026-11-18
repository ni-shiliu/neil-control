"""② 层事实到 Task / Project 候选记忆的受限投影。"""

from __future__ import annotations

from harness.memory.service import MemoryService
from harness.tasks.models import Task, TaskPlan


class TaskMemoryProjector:
    """只从已持久化的 TaskPlan 提炼决策摘要，绝不反向修改事实对象。"""

    def __init__(self, memory: MemoryService):
        self._memory = memory

    def plan_changed(self, task: Task, plan: TaskPlan) -> None:
        source = f"task_plan:{task.id}:v{plan.version}"
        if task.project_id:
            project = self._memory.create_candidate(
                scope="project", tenant_id=task.tenant_id, owner_id=task.project_id, kind="decision",
                semantic_key=f"task:{task.id}:plan", content={"task_id": task.id, "plan_version": plan.version},
                source_ref=source, write_policy="evidence_required",
            )
            self._memory.promote(project)
