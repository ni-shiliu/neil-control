"""Harness 对渠道返回的类型化交互结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ToolCallResult:
    name: str
    input: Mapping[str, Any] = field(default_factory=dict)
    result: str = ""

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ToolCallResult":
        return cls(
            name=str(value.get("name", "")),
            input=dict(value.get("input") or {}),
            result=str(value.get("result", "")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "input": dict(self.input), "result": self.result}


@dataclass(frozen=True)
class ExecutionState:
    executed: bool = False
    kind: str = ""
    success: bool | None = None
    reason: str | None = None
    agent_id: str | None = None
    tool_names: tuple[str, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ExecutionState":
        data = value or {}
        return cls(
            executed=bool(data.get("executed", False)),
            kind=str(data.get("kind", "")),
            success=data.get("success") if isinstance(data.get("success"), bool) else None,
            reason=str(data["reason"]) if data.get("reason") is not None else None,
            agent_id=str(data["agent_id"]) if data.get("agent_id") is not None else None,
            tool_names=tuple(str(item) for item in data.get("tool_calls", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"executed": self.executed, "kind": self.kind}
        if self.success is not None:
            result["success"] = self.success
        if self.reason is not None:
            result["reason"] = self.reason
        if self.agent_id is not None:
            result["agent_id"] = self.agent_id
        if self.tool_names:
            result["tool_calls"] = list(self.tool_names)
        return result


@dataclass(frozen=True)
class Interaction:
    route: str
    text: str = ""
    command: str | None = None
    tool_calls: tuple[ToolCallResult, ...] = ()
    execution: ExecutionState = field(default_factory=ExecutionState)
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_legacy(cls, value: Mapping[str, Any]) -> "Interaction":
        ai_result = value.get("ai_result")
        ai_data = dict(ai_result) if isinstance(ai_result, Mapping) else {}
        tools = tuple(ToolCallResult.from_mapping(item) for item in ai_data.pop("tool_calls", ()) if isinstance(item, Mapping))
        text = str(ai_data.pop("text", ""))
        return cls(
            route=str(value.get("route", "unknown")),
            text=text,
            command=str(value["command"]) if value.get("command") is not None else None,
            tool_calls=tools,
            execution=ExecutionState.from_mapping(value.get("execution") if isinstance(value.get("execution"), Mapping) else None),
            payload=ai_data,
        )

    @classmethod
    def coerce(cls, value: "Interaction | Mapping[str, Any]") -> "Interaction":
        return value if isinstance(value, cls) else cls.from_legacy(value)

    def to_legacy(self) -> dict[str, Any]:
        ai_result: dict[str, Any] | None = None
        if self.text or self.tool_calls or self.payload:
            ai_result = {**dict(self.payload)}
            if self.text:
                ai_result["text"] = self.text
            if self.tool_calls:
                ai_result["tool_calls"] = [call.to_dict() for call in self.tool_calls]
        return {
            "route": self.route,
            "command": self.command,
            "ai_result": ai_result,
            "execution": self.execution.to_dict(),
        }
