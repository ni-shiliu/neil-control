"""⑥ 层能力描述与执行结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from harness.governance.models import AuthorizedAction
from harness.runtime.contracts import Observation, RunRequest


ActionKind = Literal["read", "mutation", "effect"]


@dataclass(frozen=True)
class ActionManifest:
    id: str
    input_schema: Mapping[str, Any]
    description: str = ""
    output_schema: Mapping[str, Any] = field(default_factory=dict)
    required_capabilities: frozenset[str] = frozenset()
    kind: ActionKind = "read"
    risk_level: str = "low"
    auto_approve: bool = False
    produced_artifact_types: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ActionManifest id 不能为空")
        if self.input_schema.get("type", "object") != "object":
            raise ValueError("ActionManifest input_schema 必须是 object")


@dataclass(frozen=True)
class CapabilityRequest:
    run: RunRequest
    action: AuthorizedAction


@dataclass(frozen=True)
class CapabilityResult:
    content: str
    success: bool = True
    artifact_refs: tuple[str, ...] = ()
    effect_ref: str | None = None

    def observation(self, request: CapabilityRequest) -> Observation:
        return Observation(
            action_id=request.action.proposal.action_id,
            call_id=request.action.proposal.call_id,
            content=self.content,
            success=self.success,
            input=dict(request.action.proposal.input),
            artifact_refs=self.artifact_refs,
            effect_ref=self.effect_ref,
        )
