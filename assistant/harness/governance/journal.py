"""⑤ 层 Run 事件端口。Task Adapter 可把事件落入 L2 checkpoint。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from harness.runtime.contracts import RunRequest


class RunJournal(Protocol):
    def record(self, *, request: RunRequest, kind: str, metadata: dict[str, Any]) -> None: ...


class NullRunJournal:
    def record(self, *, request: RunRequest, kind: str, metadata: dict[str, Any]) -> None:
        return None


@dataclass
class InMemoryRunJournal:
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    def record(self, *, request: RunRequest, kind: str, metadata: dict[str, Any]) -> None:
        self.events.append((kind, {"run_id": request.run_id, **metadata}))


class FanoutRunJournal:
    def __init__(self, *journals: RunJournal):
        self._journals = journals

    def record(self, *, request: RunRequest, kind: str, metadata: dict[str, Any]) -> None:
        for journal in self._journals:
            journal.record(request=request, kind=kind, metadata=metadata)
