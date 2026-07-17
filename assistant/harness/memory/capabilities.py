"""⑥ 层记忆能力：候选只在当前 Run 内存中存在，校验后写入 Markdown。"""

from __future__ import annotations

from harness.capabilities import ActionManifest, CapabilityResult
from harness.memory.service import MemoryService


class MemoryProposalHandler:
    def __init__(self, service: MemoryService):
        self._service = service

    def execute(self, request) -> CapabilityResult:
        value = dict(request.action.proposal.input)
        identity = request.run.identity
        scope = str(value["scope"])
        if f"memory.{scope}" not in request.run.memory_write_scopes:
            return CapabilityResult(f"拒绝创建记忆提案：Agent 未获写入 memory.{scope} 授权", success=False)
        owners = {
            "user": identity.user_id,
            "project": identity.project_id or "",
        }
        owner = owners.get(scope, "")
        if not owner:
            return CapabilityResult("拒绝创建记忆提案：当前请求没有对应 scope owner", success=False)
        write_policy = str(value.get("write_policy", "evidence_required"))
        current_ref = f"conversation:{request.run.run_id.removeprefix('request:')}"
        if write_policy in {"explicit_preference_auto", "explicit_user_memory_auto"}:
            # 当前会话身份和 request id 是 Harness 已知的可信事实。模型可以把
            # 旧 Conversation 当作理解上下文，但不能指定写入的来源；否则一次
            # 更正很容易意外携带上一回合 source_ref 而被错误拒绝。
            source_ref = current_ref
            allowed_kinds = {"preference"} if write_policy == "explicit_preference_auto" else {"preference", "fact"}
            if scope != "user" or str(value.get("kind", "")) not in allowed_kinds:
                return CapabilityResult("拒绝自动写入：必须是当前对话中的明确用户偏好或稳定事实", success=False)
        else:
            source_ref = str(value.get("source_ref") or current_ref)
        candidate = None
        try:
            candidate = self._service.create_candidate(
                scope=scope, tenant_id=identity.tenant_id, owner_id=owner, kind=str(value["kind"]),
                semantic_key=str(value["key"]), content=dict(value["content"]),
                source_ref=source_ref, confidence=float(value.get("confidence", 1.0)),
                sensitivity=str(value["sensitivity"]), ttl=value.get("ttl"),
                write_policy=write_policy,
            )
            record = self._service.promote(candidate)
        except (TypeError, ValueError) as exc:
            if candidate is not None:
                self._service.reject(candidate, str(exc))
            return CapabilityResult(f"记忆提案未写入：{exc}", success=False)
        return CapabilityResult(f"已保存 {record.scope}/{record.kind} 记忆: {record.semantic_key}")


class MemoryForgetHandler:
    def __init__(self, service: MemoryService):
        self._service = service

    def execute(self, request) -> CapabilityResult:
        value = dict(request.action.proposal.input)
        scope = str(value["scope"])
        if f"memory.{scope}" not in request.run.memory_write_scopes:
            return CapabilityResult(f"拒绝删除：Agent 未获写入 memory.{scope} 授权", success=False)
        owner = request.run.identity.user_id if scope == "user" else request.run.identity.project_id if scope == "project" else ""
        if not owner:
            return CapabilityResult("拒绝删除：当前只允许删除自己的 user 或当前 project 记忆", success=False)
        record = self._service.forget(
            scope=scope, tenant_id=request.run.identity.tenant_id, owner_id=owner,
            semantic_key=str(value["key"]),
        )
        return CapabilityResult("记忆不存在" if record is None else f"已遗忘记忆: {record.semantic_key}")


def register_memory_actions(registry, service: MemoryService) -> None:
    registry.register(
        ActionManifest(
            id="memory_propose", kind="mutation", risk_level="low", auto_approve=True,
            description=(
                "当模型判断应保存用户的稳定事实或偏好时使用。姓名通常使用 "
                "scope=user、kind=fact、key=user.name、write_policy=explicit_user_memory_auto；"
                "更正已有记忆时直接再次调用本 action 覆盖同 key，绝不能先调用 memory_forget。"
                "自动 user 写入的来源由 Harness 绑定当前回合，不能依赖或传入旧 source_ref。不要保存推测。"
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "user 或 project"},
                    "kind": {"type": "string", "description": "user 的稳定姓名等事实使用 fact；偏好使用 preference"},
                    "key": {"type": "string", "description": "稳定语义键，例如 user.name"},
                    "content": {"type": "object", "description": "通常为 {value: 具体值}"},
                    "source_ref": {"type": "string", "description": "仅 evidence_required 的项目记忆使用"},
                    "confidence": {"type": "number"},
                    "sensitivity": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "由模型按内容敏感度判断；仅用于分类，不决定是否保存。",
                    },
                    "write_policy": {"type": "string", "description": "明确事实或偏好使用 explicit_user_memory_auto"},
                },
                "required": ["scope", "kind", "key", "content", "sensitivity", "write_policy"],
            },
        ),
        MemoryProposalHandler(service),
    )
    registry.register(
        ActionManifest(
            id="memory_forget", kind="mutation", risk_level="low", auto_approve=True,
            description="删除用户明确要求遗忘的 user 或当前 project 记忆。",
            input_schema={
                "type": "object", "properties": {"scope": {"type": "string"}, "key": {"type": "string"}},
                "required": ["scope", "key"],
            },
        ),
        MemoryForgetHandler(service),
    )
