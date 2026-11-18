"""④–⑥ 的内部受控运行器；它组合各层，不定义新的架构层。"""

from __future__ import annotations

from typing import Any, Mapping

from harness.agents.definition import AgentDefinition
from harness.capabilities.executor import CapabilityExecutor
from harness.capabilities.registry import CapabilityRegistry
from harness.governance.governor import Governor
from harness.governance.journal import NullRunJournal, RunJournal
from harness.runtime.contracts import RunRequest, RuntimeOutcome
from harness.runtime.runtime import Runtime


class ControlledRun:
    def __init__(self, *, runtime: Runtime, governor: Governor, capabilities: CapabilityRegistry, executor: CapabilityExecutor, journal: RunJournal | None = None):
        self._runtime = runtime
        self._governor = governor
        self._capabilities = capabilities
        self._executor = executor
        self._journal = journal or NullRunJournal()

    def run(self, *, agent: AgentDefinition, request: RunRequest, approvals: Mapping[str, Any] | None = None) -> RuntimeOutcome:
        preflight = self._governor.preflight(request)
        if preflight.kind != "allow":
            return self._outcome(preflight.kind, preflight.reason)
        state = self._runtime.start(agent=agent, request=request)
        policy = self._governor.profile.run_policy
        self._journal.record(
            request=request,
            kind="run_started",
            metadata={
                "scope": request.scope.kind,
                "governance_profile": self._governor.profile.id,
                "governance_version": self._governor.profile.version,
                "run_policy": {
                    "max_iterations": policy.max_iterations,
                    "max_actions": policy.max_actions,
                    "max_repeated_action": policy.max_repeated_action,
                },
                "context_budget": {
                    "window": self._governor.profile.context_budget.context_window_tokens,
                    "reserved_output": self._governor.profile.context_budget.reserved_output_tokens,
                    "soft_input": self._governor.profile.context_budget.soft_input_tokens,
                    "hard_input": self._governor.profile.context_budget.hard_input_tokens,
                    "target": self._governor.profile.context_budget.compaction_target_tokens,
                },
            },
        )
        schemas = self._capabilities.schemas_for(
            action_id for action_id in request.allowed_action_ids
            if action_id in self._capabilities.action_ids()
        )
        while True:
            usage = self._runtime.context_usage(state=state, action_schemas=schemas)
            continuation = self._governor.before_model(state, usage)
            self._journal.record(
                request=request,
                kind="context_budget_checked",
                metadata={
                    "input_tokens": usage.input_tokens,
                    "estimated": usage.estimated,
                    "decision": continuation.kind,
                    "reason": continuation.reason,
                },
            )
            if continuation.kind == "compact":
                result = self._runtime.compact(
                    state=state,
                    request=request,
                    action_schemas=schemas,
                    target_tokens=self._governor.profile.context_budget.compaction_target_tokens,
                )
                state = result.state
                self._journal.record(
                    request=request,
                    kind="context_compacted",
                    metadata={
                        "mode": result.mode,
                        "before_tokens": result.before.input_tokens,
                        "after_tokens": result.after.input_tokens,
                        "estimated_before": result.before.estimated,
                        "estimated_after": result.after.estimated,
                        "compressed_messages": result.compressed_messages,
                        "input_compacted": result.input_compacted,
                        "summary": state.run_summary.render() if state.run_summary else "",
                        "source_refs": state.run_summary.source_refs if state.run_summary else (),
                    },
                )
                budget = self._governor.profile.context_budget
                # 压缩后仍不能跨过软阈值，说明只剩不可裁剪锚点或系统/工具
                # 定义；继续循环只会反复压缩同一份内容，须可审计地中断。
                if result.after.input_tokens >= budget.soft_input_tokens:
                    return self._outcome(
                        "interrupt",
                        "context_unrecoverable: 压缩与强制裁剪后仍无法满足上下文预算",
                        state=state,
                    )
                continue
            if continuation.kind != "allow":
                return self._outcome(continuation.kind, continuation.reason, state=state)
            decision, state = self._runtime.decide(state=state, action_schemas=schemas)
            self._journal.record(
                request=request,
                kind="model_decision",
                metadata={
                    "iteration": state.iteration,
                    "kind": decision.kind,
                    "action_ids": tuple(item.action_id for item in decision.actions),
                },
            )
            if decision.kind == "error":
                return RuntimeOutcome("failed", state=state, reason=decision.error)
            if decision.kind == "final":
                output = self._governor.inspect_output(decision.text)
                if output.kind == "finish":
                    return RuntimeOutcome("completed", text=decision.text, state=state, observations=state.observations)
                return self._outcome(output.kind, output.reason, state=state)

            authorization = self._governor.authorize_actions(
                request=request, state=state, proposals=decision.actions,
                actions=self._capabilities, approvals=approvals,
            )
            self._journal.record(
                request=request,
                kind="action_decision",
                metadata={
                    "kind": authorization.kind,
                    "reason": authorization.reason,
                    "action_ids": tuple(item.proposal.action_id for item in authorization.authorized_actions),
                },
            )
            if authorization.kind != "allow":
                return self._outcome(authorization.kind, authorization.reason, state=state)
            observations = tuple(self._executor.execute(run=request, action=action) for action in authorization.authorized_actions)
            self._journal.record(
                request=request,
                kind="observations_recorded",
                metadata={
                    "count": len(observations),
                    "success": all(item.success for item in observations),
                    "actions": tuple({
                        "id": item.action_id,
                        "success": item.success,
                        "result": item.content,
                    } for item in observations),
                },
            )
            state = self._runtime.observe(state, observations)

    @staticmethod
    def _outcome(kind: str, reason: str, *, state=None) -> RuntimeOutcome:
        status = {"deny": "denied", "interrupt": "interrupted", "finish": "completed"}.get(kind, "failed")
        return RuntimeOutcome(status, state=state, reason=reason)
