"""Task 数据平面的 JSON Repository。"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harness.tasks.models import (
    Artifact, Checkpoint, EffectReference, PlanNode, Run, Task, TaskPlan, TaskRequestLink,
    as_jsonable,
)

class TaskRepository:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path(__file__).with_name("task_store")

    def _task_dir(self, task_id: str) -> Path:
        return self.base_dir / "tasks" / task_id

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise KeyError(f"不存在的 Task 数据: {path}") from exc

    def save_task(self, task: Task) -> None:
        self._write_json(self._task_dir(task.id) / "task.json", as_jsonable(task))

    def get_task(self, task_id: str) -> Task:
        payload = self._read_json(self._task_dir(task_id) / "task.json")
        payload["acceptance_criteria"] = tuple(payload["acceptance_criteria"])
        payload["constraints"] = tuple(payload.get("constraints", ()))
        return Task(**payload)

    def list_task_request_links(self, task_id: str) -> list[TaskRequestLink]:
        directory = self._task_dir(task_id) / "requests"
        if not directory.exists():
            return []
        return [TaskRequestLink(**self._read_json(path)) for path in sorted(directory.glob("*.json"))]

    def save_task_request_link(self, link: TaskRequestLink) -> None:
        self._write_json(
            self._task_dir(link.task_id) / "requests" / f"{link.request_id}.json",
            as_jsonable(link),
        )

    def save_plan(self, plan: TaskPlan) -> None:
        self._write_json(self._task_dir(plan.task_id) / "plans" / f"v{plan.version}.json", as_jsonable(plan))

    def get_plan(self, task_id: str, version: int) -> TaskPlan:
        payload = self._read_json(self._task_dir(task_id) / "plans" / f"v{version}.json")
        payload["nodes"] = tuple(PlanNode(
            id=node["id"], title=node["title"], description=node["description"],
            depends_on=tuple(node.get("depends_on", ())),
            acceptance_criteria=tuple(node.get("acceptance_criteria", ())),
        ) for node in payload["nodes"])
        payload["acceptance_criteria"] = tuple(payload["acceptance_criteria"])
        return TaskPlan(**payload)

    def save_run(self, run: Run) -> None:
        self._write_json(self._task_dir(run.task_id) / "runs" / f"{run.id}.json", as_jsonable(run))

    def get_run(self, task_id: str, run_id: str) -> Run:
        return Run(**self._read_json(self._task_dir(task_id) / "runs" / f"{run_id}.json"))

    def list_runs(self, task_id: str) -> list[Run]:
        directory = self._task_dir(task_id) / "runs"
        if not directory.exists():
            return []
        return [Run(**self._read_json(path)) for path in sorted(directory.glob("*.json"))]

    def save_artifact(self, artifact: Artifact) -> None:
        self._write_json(
            self._task_dir(artifact.task_id) / "artifacts" / artifact.id / f"v{artifact.version}.json",
            as_jsonable(artifact),
        )

    def get_artifact(self, task_id: str, artifact_id: str, version: int = 1) -> Artifact:
        return Artifact(**self._read_json(
            self._task_dir(task_id) / "artifacts" / artifact_id / f"v{version}.json"
        ))

    def list_artifact_versions(self, task_id: str, artifact_id: str) -> list[Artifact]:
        directory = self._task_dir(task_id) / "artifacts" / artifact_id
        if not directory.exists():
            return []
        return [Artifact(**self._read_json(path)) for path in sorted(directory.glob("v*.json"))]

    def save_effect_reference(self, effect: EffectReference) -> None:
        self._write_json(self._task_dir(effect.task_id) / "effects" / f"{effect.id}.json", as_jsonable(effect))

    def get_effect_reference(self, task_id: str, effect_id: str) -> EffectReference:
        return EffectReference(**self._read_json(self._task_dir(task_id) / "effects" / f"{effect_id}.json"))

    def save_checkpoint(self, checkpoint: Checkpoint) -> None:
        self._write_json(self._task_dir(checkpoint.task_id) / "checkpoints" / f"{checkpoint.id}.json", as_jsonable(checkpoint))

    def get_checkpoint(self, task_id: str, checkpoint_id: str) -> Checkpoint:
        return Checkpoint(**self._read_json(self._task_dir(task_id) / "checkpoints" / f"{checkpoint_id}.json"))
