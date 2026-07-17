"""⑥ 层执行器：只消费⑤层签发的 AuthorizedAction。"""

from __future__ import annotations

from harness.capabilities.effects import EffectRecord, EffectOutbox, InMemoryEffectOutbox
from harness.capabilities.models import CapabilityRequest, CapabilityResult
from harness.capabilities.registry import CapabilityRegistry
from harness.governance.models import AuthorizedAction
from harness.runtime.contracts import Observation, RunRequest


class CapabilityExecutor:
    def __init__(self, *, registry: CapabilityRegistry, outbox: EffectOutbox | None = None):
        self._registry = registry
        self._outbox = outbox or InMemoryEffectOutbox()

    def execute(self, *, run: RunRequest, action: AuthorizedAction) -> Observation:
        try:
            manifest = self._registry.get_manifest(action.proposal.action_id)
        except KeyError:
            return Observation(
                action_id=action.proposal.action_id, call_id=action.proposal.call_id,
                content=f"拒绝执行：未知 action {action.proposal.action_id}", success=False,
                input=dict(action.proposal.input),
            )
        try:
            self._validate_input(action.proposal.input, manifest.input_schema)
        except ValueError as exc:
            return Observation(
                action_id=action.proposal.action_id, call_id=action.proposal.call_id,
                content=f"拒绝执行：{exc}", success=False, input=dict(action.proposal.input),
            )
        request = CapabilityRequest(run=run, action=action)
        if manifest.kind != "effect":
            return self._registry.get_handler(manifest.id).execute(request).observation(request)

        previous = self._outbox.get(action.idempotency_key)
        if previous is not None and previous.status == "succeeded":
            return CapabilityResult(
                content=previous.result, success=True, effect_ref=previous.idempotency_key,
            ).observation(request)
        self._outbox.save(EffectRecord(action.idempotency_key, manifest.id, "pending"))
        try:
            result = self._registry.get_handler(manifest.id).execute(request)
        except Exception as exc:
            self._outbox.save(EffectRecord(action.idempotency_key, manifest.id, "failed", str(exc)))
            return CapabilityResult(content=str(exc), success=False, effect_ref=action.idempotency_key).observation(request)
        status = "succeeded" if result.success else "failed"
        self._outbox.save(EffectRecord(action.idempotency_key, manifest.id, status, result.content))
        return CapabilityResult(
            content=result.content, success=result.success,
            artifact_refs=result.artifact_refs, effect_ref=action.idempotency_key,
        ).observation(request)

    @staticmethod
    def _validate_input(value: object, schema: object) -> None:
        if not isinstance(value, dict) or not isinstance(schema, dict):
            raise ValueError("action input 必须是 object")
        required = schema.get("required", ())
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise ValueError("action schema properties 无效")
        for name in required:
            if name not in value:
                raise ValueError(f"action input 缺少字段: {name}")
        for name, item in value.items():
            if name not in properties:
                raise ValueError(f"action input 包含未知字段: {name}")
            expected = properties[name].get("type") if isinstance(properties[name], dict) else None
            if expected == "string" and not isinstance(item, str):
                raise ValueError(f"action input 字段 {name} 必须是 string")
            if expected == "integer" and (not isinstance(item, int) or isinstance(item, bool)):
                raise ValueError(f"action input 字段 {name} 必须是 integer")
            if expected == "number" and (not isinstance(item, (int, float)) or isinstance(item, bool)):
                raise ValueError(f"action input 字段 {name} 必须是 number")
            if expected == "boolean" and not isinstance(item, bool):
                raise ValueError(f"action input 字段 {name} 必须是 boolean")
            if expected == "object" and not isinstance(item, dict):
                raise ValueError(f"action input 字段 {name} 必须是 object")
            if expected == "array" and not isinstance(item, list):
                raise ValueError(f"action input 字段 {name} 必须是 array")
