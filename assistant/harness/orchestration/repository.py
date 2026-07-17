"""③层对象在 Task 数据目录中的原子 JSON 存储。"""

from __future__ import annotations

from pathlib import Path

from harness.orchestration.models import (
    EffectIntent, ExecutionPlan, ReviewVerdict, WorkOrder, as_jsonable,
    effect_intent_from_dict, execution_plan_from_dict, review_verdict_from_dict, work_order_from_dict,
)
from harness.tasks.repository import TaskRepository


class OrchestrationRepository:
    def __init__(self, task_repository: TaskRepository):
        self._tasks = task_repository

    def _directory(self, task_id: str, kind: str) -> Path:
        return self._tasks.base_dir / "tasks" / task_id / kind

    def save_execution_plan(self, plan: ExecutionPlan) -> None:
        self._tasks._write_json(self._directory(plan.task_id, "execution_plans") / f"{plan.id}.json", as_jsonable(plan))

    def get_execution_plan(self, task_id: str, execution_plan_id: str) -> ExecutionPlan:
        return execution_plan_from_dict(self._tasks._read_json(
            self._directory(task_id, "execution_plans") / f"{execution_plan_id}.json"
        ))

    def list_execution_plans(self, task_id: str) -> list[ExecutionPlan]:
        directory = self._directory(task_id, "execution_plans")
        if not directory.exists():
            return []
        return [execution_plan_from_dict(self._tasks._read_json(path)) for path in sorted(directory.glob("*.json"))]

    def save_work_order(self, order: WorkOrder) -> None:
        self._tasks._write_json(self._directory(order.task_id, "work_orders") / f"{order.id}.json", as_jsonable(order))

    def get_work_order(self, task_id: str, work_order_id: str) -> WorkOrder:
        return work_order_from_dict(self._tasks._read_json(
            self._directory(task_id, "work_orders") / f"{work_order_id}.json"
        ))

    def list_work_orders(self, task_id: str, *, execution_plan_id: str | None = None) -> list[WorkOrder]:
        directory = self._directory(task_id, "work_orders")
        if not directory.exists():
            return []
        orders = [work_order_from_dict(self._tasks._read_json(path)) for path in sorted(directory.glob("*.json"))]
        return [item for item in orders if item.execution_plan_id == execution_plan_id] if execution_plan_id else orders

    def save_review_verdict(self, verdict: ReviewVerdict) -> None:
        self._tasks._write_json(self._directory(verdict.task_id, "review_verdicts") / f"{verdict.id}.json", as_jsonable(verdict))

    def get_review_verdict(self, task_id: str, verdict_id: str) -> ReviewVerdict:
        return review_verdict_from_dict(self._tasks._read_json(
            self._directory(task_id, "review_verdicts") / f"{verdict_id}.json"
        ))

    def save_effect_intent(self, intent: EffectIntent) -> None:
        self._tasks._write_json(self._directory(intent.task_id, "effect_intents") / f"{intent.id}.json", as_jsonable(intent))

    def get_effect_intent(self, task_id: str, intent_id: str) -> EffectIntent:
        return effect_intent_from_dict(self._tasks._read_json(
            self._directory(task_id, "effect_intents") / f"{intent_id}.json"
        ))
