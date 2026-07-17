"""L2 对⑤ RunJournal 的持久化适配器。"""

from __future__ import annotations

from typing import Any

from harness.runtime.contracts import RunRequest
from harness.tasks.service import TaskService


class TaskRunJournal:
    """只把运行事件写成 checkpoint；不参与 Runtime 决策。"""

    def __init__(self, service: TaskService):
        self._service = service

    def record(self, *, request: RunRequest, kind: str, metadata: dict[str, Any]) -> None:
        if not request.scope.task_id:
            return
        self._service.record_checkpoint(
            request.scope.task_id,
            kind=f"runtime_{kind}",
            run_id=request.run_id,
            metadata={"scope": request.scope.kind, **metadata},
        )
