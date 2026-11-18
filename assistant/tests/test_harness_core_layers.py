from __future__ import annotations

from pathlib import Path

from harness.agents.chat import CHAT_AGENT
from harness.capabilities import (
    ActionManifest, CapabilityExecutor, CapabilityRegistry, CapabilityResult,
    EffectRecord, InMemoryEffectOutbox,
)
from harness.governance import DEFAULT_GOVERNANCE_PROFILE, AuthorizedAction, GovernanceProfile, Governor, RunPolicy
from harness.runtime import (
    ActionProposal, ContextAssembler, ControlledRun, ModelResponse, RunRequest,
    Runtime,
)


class FakeModel:
    def __init__(self, responses: list[ModelResponse]):
        self.responses = list(responses)
        self.requests = []

    def complete(self, *, system_prompt, messages, action_schemas):
        self.requests.append((system_prompt, tuple(messages), tuple(action_schemas)))
        return self.responses.pop(0)

    def complete_json(self, *, system_prompt, user_input):
        return {"kind": "ordinary"}


class Handler:
    def __init__(self):
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        return CapabilityResult(f"observed {request.action.proposal.input['value']}")


def _run_request(*, actions: frozenset[str]) -> RunRequest:
    return RunRequest(
        run_id="run_test", agent_id=CHAT_AGENT.id, agent_version=CHAT_AGENT.version,
        channel="test", user_input="hello", allowed_action_ids=actions,
    )


def test_controlled_run_cycles_model_governance_capability_and_observation() -> None:
    model = FakeModel([
        ModelResponse(actions=(ActionProposal("call_1", "read.test", {"value": "fact"}),), stop_reason="tool_use"),
        ModelResponse(text="final answer"),
    ])
    handler = Handler()
    registry = CapabilityRegistry((
        (ActionManifest("read.test", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}), handler),
    ))
    outcome = ControlledRun(
        runtime=Runtime(model=model, context=ContextAssembler()),
        governor=Governor(profile=DEFAULT_GOVERNANCE_PROFILE),
        capabilities=registry,
        executor=CapabilityExecutor(registry=registry),
    ).run(agent=CHAT_AGENT, request=_run_request(actions=frozenset({"read.test"})))

    assert outcome.status == "completed"
    assert outcome.text == "final answer"
    assert handler.calls == 1
    assert outcome.observations[0].content == "observed fact"
    assert model.requests[1][1][-1].observations[0].content == "observed fact"


def test_mutation_interrupts_before_handler_execution() -> None:
    model = FakeModel([ModelResponse(actions=(ActionProposal("call_1", "write.test", {"value": "x"}),), stop_reason="tool_use")])
    handler = Handler()
    registry = CapabilityRegistry((
        (ActionManifest("write.test", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}, kind="mutation"), handler),
    ))
    outcome = ControlledRun(
        runtime=Runtime(model=model, context=ContextAssembler()),
        governor=Governor(profile=DEFAULT_GOVERNANCE_PROFILE),
        capabilities=registry,
        executor=CapabilityExecutor(registry=registry),
    ).run(agent=CHAT_AGENT, request=_run_request(actions=frozenset({"write.test"})))

    assert outcome.status == "interrupted"
    assert handler.calls == 0


def test_ungranted_or_invalid_action_is_rejected_without_handler_execution() -> None:
    model = FakeModel([ModelResponse(actions=(ActionProposal("call_1", "read.test", {"value": 3}),), stop_reason="tool_use")])
    handler = Handler()
    registry = CapabilityRegistry((
        (ActionManifest("read.test", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}), handler),
    ))
    outcome = ControlledRun(
        runtime=Runtime(model=model, context=ContextAssembler()),
        governor=Governor(profile=DEFAULT_GOVERNANCE_PROFILE),
        capabilities=registry,
        executor=CapabilityExecutor(registry=registry),
    ).run(agent=CHAT_AGENT, request=_run_request(actions=frozenset()))
    assert outcome.status == "denied"
    assert handler.calls == 0

    invalid = CapabilityExecutor(registry=registry).execute(
        run=_run_request(actions=frozenset({"read.test"})),
        action=AuthorizedAction(ActionProposal("call_2", "read.test", {"value": 3}), "invalid"),
    )
    assert invalid.success is False
    assert "拒绝执行" in invalid.content
    assert handler.calls == 0


def test_effect_executor_uses_idempotency_key() -> None:
    handler = Handler()
    registry = CapabilityRegistry((
        (ActionManifest("effect.test", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}, kind="effect"), handler),
    ))
    outbox = InMemoryEffectOutbox()
    executor = CapabilityExecutor(registry=registry, outbox=outbox)
    proposal = ActionProposal("call_1", "effect.test", {"value": "send"})
    action = AuthorizedAction(proposal=proposal, idempotency_key="stable")
    request = _run_request(actions=frozenset({"effect.test"}))

    first = executor.execute(run=request, action=action)
    second = executor.execute(run=request, action=action)

    assert first.success is second.success is True
    assert handler.calls == 1
    assert outbox.get("stable") == EffectRecord("stable", "effect.test", "succeeded", "observed send")


def test_iteration_budget_interrupts_the_run() -> None:
    model = FakeModel([ModelResponse(actions=(ActionProposal("call_1", "read.test", {"value": "fact"}),), stop_reason="tool_use")])
    handler = Handler()
    registry = CapabilityRegistry((
        (ActionManifest("read.test", {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}), handler),
    ))
    outcome = ControlledRun(
        runtime=Runtime(model=model, context=ContextAssembler()),
        governor=Governor(profile=GovernanceProfile("limited", "1", RunPolicy(max_iterations=1))),
        capabilities=registry,
        executor=CapabilityExecutor(registry=registry),
    ).run(agent=CHAT_AGENT, request=_run_request(actions=frozenset({"read.test"})))

    assert outcome.status == "interrupted"
    assert "最大模型轮数" in (outcome.reason or "")


def test_new_runtime_core_does_not_import_engine() -> None:
    root = Path(__file__).parent.parent / "harness"
    for path in (
        root / "runtime" / "contracts.py", root / "runtime" / "context.py",
        root / "runtime" / "model.py", root / "runtime" / "runtime.py",
        root / "runtime" / "runner.py", root / "governance" / "governor.py",
        root / "capabilities" / "executor.py",
    ):
        assert "engine." not in path.read_text(encoding="utf-8")


def test_chat_browser_adapter_registers_existing_capability_without_core_dependency() -> None:
    from harness.agents.chat.capabilities import register_browser_actions

    registry = CapabilityRegistry()
    register_browser_actions(registry)

    assert registry.get_manifest("browser_open_url").required_capabilities == frozenset({"browser"})
    assert registry.get_manifest("browser_click_text").kind == "mutation"
