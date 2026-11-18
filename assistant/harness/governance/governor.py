"""⑤ 层治理器：不调用模型、不执行能力。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Protocol

from harness.governance.models import AuthorizedAction, GovernanceDecision, GovernanceProfile
from harness.runtime.contracts import ActionProposal, ContextUsage, RunRequest, RuntimeState


class ActionPolicyView(Protocol):
    def get_manifest(self, action_id: str): ...


class Governor:
    def __init__(self, *, profile: GovernanceProfile, environment_capabilities: Iterable[str] = ()):
        self._profile = profile
        self._environment_capabilities = frozenset(environment_capabilities)

    @property
    def profile(self) -> GovernanceProfile:
        return self._profile

    def preflight(self, request: RunRequest) -> GovernanceDecision:
        if not request.user_input.strip():
            return GovernanceDecision("deny", "输入不能为空")
        return GovernanceDecision("allow")

    def before_model(self, state: RuntimeState, usage: ContextUsage | None = None) -> GovernanceDecision:
        if state.iteration >= self._profile.run_policy.max_iterations:
            return GovernanceDecision("interrupt", "达到最大模型轮数")
        if usage is not None:
            budget = self._profile.context_budget
            if usage.input_tokens >= budget.hard_input_tokens:
                return GovernanceDecision(
                    "compact",
                    f"输入上下文达到硬上限: {usage.input_tokens}/{budget.hard_input_tokens}",
                )
            if usage.input_tokens >= budget.soft_input_tokens:
                return GovernanceDecision(
                    "compact",
                    f"输入上下文达到预压缩阈值: {usage.input_tokens}/{budget.soft_input_tokens}",
                )
        return GovernanceDecision("allow")

    def authorize_actions(
        self,
        *,
        request: RunRequest,
        state: RuntimeState,
        proposals: tuple[ActionProposal, ...],
        actions: ActionPolicyView,
        approvals: Mapping[str, object] | None = None,
    ) -> GovernanceDecision:
        if len(proposals) + sum(1 for _ in state.observations) > self._profile.run_policy.max_actions:
            return GovernanceDecision("interrupt", "达到最大动作预算")
        approved_ids = frozenset(str(item) for item in (approvals or {}).get("approved_action_ids", ()))
        authorized: list[AuthorizedAction] = []
        fingerprints: list[str] = []
        for proposal in proposals:
            if proposal.action_id not in request.allowed_action_ids:
                return GovernanceDecision("deny", f"未授予 action: {proposal.action_id}")
            try:
                manifest = actions.get_manifest(proposal.action_id)
            except KeyError:
                return GovernanceDecision("deny", f"未知 action: {proposal.action_id}")
            if not manifest.required_capabilities <= self._environment_capabilities:
                return GovernanceDecision("deny", f"环境缺少 capability: {proposal.action_id}")
            fingerprint = self._fingerprint(proposal)
            fingerprints.append(fingerprint)
            repeated = sum(
                1 for observation in state.observations
                if observation.call_id == proposal.call_id or observation.action_id == proposal.action_id
            )
            if repeated >= self._profile.run_policy.max_repeated_action:
                return GovernanceDecision("interrupt", f"重复动作超过阈值: {proposal.action_id}")
            if manifest.kind == "mutation" and not (self._profile.auto_approve_mutations or getattr(manifest, "auto_approve", False)) and proposal.call_id not in approved_ids:
                return GovernanceDecision("interrupt", f"等待批准状态修改: {proposal.action_id}")
            if manifest.kind == "effect" and not (self._profile.auto_approve_effects or getattr(manifest, "auto_approve", False)) and proposal.call_id not in approved_ids:
                return GovernanceDecision("interrupt", f"等待批准外部 Effect: {proposal.action_id}")
            if manifest.kind == "read" and not self._profile.auto_approve_readonly:
                return GovernanceDecision("interrupt", f"等待批准读取动作: {proposal.action_id}")
            key = hashlib.sha256(f"{request.run_id}:{proposal.call_id}:{fingerprint}".encode()).hexdigest()
            authorized.append(AuthorizedAction(proposal=proposal, idempotency_key=key))
        if len(set(fingerprints)) != len(fingerprints):
            return GovernanceDecision("interrupt", "同一轮包含重复动作")
        return GovernanceDecision("allow", authorized_actions=tuple(authorized))

    @staticmethod
    def inspect_output(text: str) -> GovernanceDecision:
        if not text.strip():
            return GovernanceDecision("deny", "模型未生成可交付输出")
        return GovernanceDecision("finish")

    @staticmethod
    def _fingerprint(proposal: ActionProposal) -> str:
        return f"{proposal.action_id}:{json.dumps(dict(proposal.input), ensure_ascii=False, sort_keys=True)}"
