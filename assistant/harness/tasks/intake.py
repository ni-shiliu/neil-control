"""②层的 Task/Plan 意图数据契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from harness.tasks.models import PlanPatch, TaskProposal


TaskIntentKind = Literal["ordinary", "create_task", "patch_active_task", "clarify"]


@dataclass(frozen=True)
class TaskIntakeDecision:
    """Runtime 对单个请求提出的 Task 意图；本身不产生持久化副作用。"""

    kind: TaskIntentKind
    proposal: TaskProposal | None = None
    patch: PlanPatch | None = None
    clarification: str | None = None
