"""模型供应商端口；其响应在进入 Harness 前被规范化。"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from harness.runtime.contracts import ActionProposal, ContextUsage, ModelMessage, ModelResponse


class ModelGateway(Protocol):
    def complete(
        self,
        *,
        system_prompt: str,
        messages: Sequence[ModelMessage],
        action_schemas: Sequence[Mapping[str, Any]],
    ) -> ModelResponse: ...

    def complete_json(self, *, system_prompt: str, user_input: str) -> dict[str, Any]: ...


class AnthropicModelGateway:
    """Anthropic 的 L4 适配器；其他层不接触 SDK 对象。"""

    def __init__(self, client: Any | None = None, model: str | None = None):
        self._client = client
        self._model = model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    def _client_or_create(self) -> Any:
        if self._client is None:
            import anthropic

            kwargs: dict[str, Any] = {
                "api_key": os.environ.get("ANTHROPIC_API_KEY", "placeholder"),
                "timeout": float(os.environ.get("ANTHROPIC_TIMEOUT_SECONDS", "90")),
            }
            if base_url := os.environ.get("ANTHROPIC_BASE_URL"):
                kwargs["base_url"] = base_url
            if auth_token := os.environ.get("ANTHROPIC_AUTH_TOKEN"):
                kwargs["default_headers"] = {"Authorization": f"Bearer {auth_token}"}
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def complete(self, *, system_prompt: str, messages: Sequence[ModelMessage], action_schemas: Sequence[Mapping[str, Any]]) -> ModelResponse:
        response = self._client_or_create().messages.create(
            model=self._model,
            max_tokens=2048,
            system=system_prompt,
            tools=list(action_schemas),
            messages=[self._message(item) for item in messages],
        )
        text: list[str] = []
        actions: list[ActionProposal] = []
        for block in getattr(response, "content", ()):
            block_type = getattr(block, "type", "")
            if block_type == "text":
                text.append(str(getattr(block, "text", "")))
            elif block_type == "tool_use":
                actions.append(ActionProposal(
                    call_id=str(getattr(block, "id", "")),
                    action_id=str(getattr(block, "name", "")),
                    input=dict(getattr(block, "input", {}) or {}),
                ))
        return ModelResponse(text="\n".join(item for item in text if item), actions=tuple(actions), stop_reason=str(getattr(response, "stop_reason", "")))

    def count_input_tokens(
        self,
        *,
        system_prompt: str,
        messages: Sequence[ModelMessage],
        action_schemas: Sequence[Mapping[str, Any]],
    ) -> ContextUsage:
        """使用与实际 Messages 请求同形的 Anthropic token-counting 调用。"""
        response = self._client_or_create().messages.count_tokens(
            model=self._model,
            system=system_prompt,
            tools=list(action_schemas),
            messages=[self._message(item) for item in messages],
        )
        return ContextUsage(input_tokens=int(getattr(response, "input_tokens", 0)), estimated=False)

    @staticmethod
    def _message(item: ModelMessage) -> dict[str, Any]:
        if item.role == "assistant" and item.actions:
            blocks: list[dict[str, Any]] = []
            if item.content:
                blocks.append({"type": "text", "text": item.content})
            blocks.extend({
                "type": "tool_use", "id": action.call_id, "name": action.action_id,
                "input": dict(action.input),
            } for action in item.actions)
            return {"role": item.role, "content": blocks}
        if item.role == "user" and item.observations:
            return {
                "role": item.role,
                "content": [{
                    "type": "tool_result", "tool_use_id": observation.call_id,
                    "content": observation.content,
                    "is_error": not observation.success,
                } for observation in item.observations],
            }
        return {"role": item.role, "content": item.content}

    def complete_json(self, *, system_prompt: str, user_input: str) -> dict[str, Any]:
        response = self.complete(
            system_prompt=f"{system_prompt}\n\n只输出合法 JSON，不要 Markdown。",
            messages=(ModelMessage("user", user_input),),
            action_schemas=(),
        )
        value = json.loads(response.text)
        if not isinstance(value, dict):
            raise ValueError("模型 JSON 顶层必须为 object")
        return value
