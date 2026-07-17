from __future__ import annotations

import json
from pathlib import Path

from harness.agents.chat import CHAT_AGENT
from harness.capabilities import ActionManifest, CapabilityExecutor, CapabilityRegistry, CapabilityResult
from harness.governance import ContextBudget, GovernanceProfile, Governor, InMemoryRunJournal
from harness.runtime import (
    ActionProposal, ContextAssembler, ContextUsage, ControlledRun, GatewayTokenCounter,
    ContextSnapshot, ModelMessage, ModelResponse, RunRequest, Runtime, RuntimeCompactor, RuntimeState,
)
from harness.runtime.contracts import RunScope
from harness.tasks import PlanNode, TaskProposal, TaskRepository, TaskRunJournal, TaskService


class BudgetModel:
    def __init__(self, responses: list[ModelResponse], *, fail_summary: bool = False):
        self.responses = list(responses)
        self.requests = []
        self.summary_calls = 0
        self.fail_summary = fail_summary

    def complete(self, *, system_prompt, messages, action_schemas):
        self.requests.append((system_prompt, tuple(messages), tuple(action_schemas)))
        return self.responses.pop(0)

    def complete_json(self, *, system_prompt, user_input):
        self.summary_calls += 1
        if self.fail_summary:
            raise RuntimeError("summary unavailable")
        return {
            "objective": "finish the task",
            "constraints": ["preserve artifacts"],
            "completed_work": ["read prior observation"],
            "decisions": ["continue"],
            "observations": ["artifact:one"],
            "open_items": ["answer user"],
            "next_step": "respond",
        }


class ThresholdCounter:
    """让测试只关注预算分支，不依赖具体 tokenizer。"""

    def __init__(self, *, initial: int = 100, observed: int = 900_000):
        self.initial = initial
        self.observed = observed
        self.calls = []

    def count(self, *, system_prompt, messages, action_schemas):
        self.calls.append((system_prompt, tuple(messages), tuple(action_schemas)))
        content = "\n".join(message.content for message in messages)
        if messages and len(messages[0].content) > 1_000_000:
            return ContextUsage(self.initial)
        if "[当前用户输入已分块压缩" in content or "[此前 Run 历史摘要" in content:
            return ContextUsage(500_000)
        if any(message.observations for message in messages):
            return ContextUsage(self.observed)
        return ContextUsage(self.initial)


class ReadHandler:
    def execute(self, _request):
        return CapabilityResult("tool result")


def _request(user_input: str = "hello") -> RunRequest:
    return RunRequest(
        run_id="request:context", agent_id=CHAT_AGENT.id, agent_version=CHAT_AGENT.version,
        channel="test", user_input=user_input, allowed_action_ids=frozenset({"read.test"}),
    )


def _registry() -> CapabilityRegistry:
    return CapabilityRegistry((
        (ActionManifest("read.test", {"type": "object", "properties": {}, "required": []}), ReadHandler()),
    ))


def _profile() -> GovernanceProfile:
    return GovernanceProfile(
        "context", "1",
        context_budget=ContextBudget(
            context_window_tokens=1_000_000,
            reserved_output_tokens=32_000,
            soft_input_tokens=800_000,
            compaction_target_tokens=600_000,
        ),
    )


def test_soft_threshold_compacts_history_and_journals_usage() -> None:
    model = BudgetModel([
        ModelResponse(actions=(ActionProposal("call", "read.test"),), stop_reason="tool_use"),
        ModelResponse(text="done"),
    ])
    counter = ThresholdCounter(observed=850_000)
    journal = InMemoryRunJournal()
    outcome = ControlledRun(
        runtime=Runtime(model=model, context=ContextAssembler(), token_counter=counter),
        governor=Governor(profile=_profile()), capabilities=_registry(),
        executor=CapabilityExecutor(registry=_registry()), journal=journal,
    ).run(agent=CHAT_AGENT, request=_request())

    assert outcome.status == "completed"
    assert model.summary_calls == 1
    assert any(kind == "context_compacted" for kind, _ in journal.events)
    compacted = next(data for kind, data in journal.events if kind == "context_compacted")
    assert compacted["before_tokens"] == 850_000
    assert compacted["after_tokens"] == 500_000
    assert "finish the task" in compacted["summary"]


def test_hard_limit_compacts_before_any_model_decision() -> None:
    model = BudgetModel([ModelResponse(text="done")])
    counter = ThresholdCounter(initial=968_000)
    runtime = Runtime(model=model, context=ContextAssembler(), token_counter=counter)
    outcome = ControlledRun(
        runtime=runtime, governor=Governor(profile=_profile()), capabilities=_registry(),
        executor=CapabilityExecutor(registry=_registry()),
    ).run(agent=CHAT_AGENT, request=_request())

    assert outcome.status == "completed"
    assert len(model.requests) == 1
    assert "[此前 Run 历史摘要" in model.requests[0][1][-1].content


def test_oversized_current_input_is_chunked_without_mutating_request() -> None:
    original = "x" * 1_100_000
    model = BudgetModel([ModelResponse(text="done")])
    counter = ThresholdCounter(initial=1_000_000)
    outcome = ControlledRun(
        runtime=Runtime(model=model, context=ContextAssembler(), token_counter=counter),
        governor=Governor(profile=_profile()), capabilities=_registry(),
        executor=CapabilityExecutor(registry=_registry()),
    ).run(agent=CHAT_AGENT, request=_request(original))

    assert outcome.status == "completed"
    assert model.summary_calls >= 1
    assert outcome.state is not None
    assert outcome.state.original_input_ref == "conversation:context"
    assert original == "x" * 1_100_000
    assert model.requests[0][1][0].content.startswith("[当前用户输入已分块压缩")


def test_summary_failure_uses_deterministic_forced_trimming() -> None:
    model = BudgetModel([
        ModelResponse(actions=(ActionProposal("call", "read.test"),), stop_reason="tool_use"),
        ModelResponse(text="done"),
    ], fail_summary=True)
    journal = InMemoryRunJournal()
    outcome = ControlledRun(
        runtime=Runtime(model=model, context=ContextAssembler(), token_counter=ThresholdCounter(observed=850_000)),
        governor=Governor(profile=_profile()), capabilities=_registry(),
        executor=CapabilityExecutor(registry=_registry()), journal=journal,
    ).run(agent=CHAT_AGENT, request=_request())

    assert outcome.status == "completed"
    compacted = next(data for kind, data in journal.events if kind == "context_compacted")
    assert compacted["mode"] == "deterministic"


def test_gateway_counter_includes_system_messages_and_tool_schemas() -> None:
    class Gateway:
        def count_input_tokens(self, *, system_prompt, messages, action_schemas):
            assert system_prompt == "system"
            assert messages[0].content == "user"
            assert action_schemas[0]["name"] == "tool"
            return 123

    usage = GatewayTokenCounter(Gateway()).count(
        system_prompt="system",
        messages=(ModelMessage("user", "user"),),
        action_schemas=({"name": "tool"},),
    )
    assert usage == ContextUsage(123)


def test_task_journal_persists_context_compaction_checkpoint(tmp_path: Path) -> None:
    service = TaskService(TaskRepository(tmp_path / "tasks"))
    from harness.channels import create_incoming_request

    task = service.create_task(
        TaskProposal(
            title="t", objective="o", acceptance_criteria=("done",),
            nodes=(PlanNode("n", "n", "work", (), ("done",)),),
        ),
        request=create_incoming_request(channel="cli", raw_text="task"), agent=CHAT_AGENT,
    )
    TaskRunJournal(service).record(
        request=RunRequest(
            run_id="run-context", agent_id="chat", agent_version="1", channel="cli", user_input="x",
            scope=RunScope(kind="work_order", task_id=task.id),
        ),
        kind="context_compacted",
        metadata={"before_tokens": 850_000, "after_tokens": 500_000, "summary": "compressed"},
    )
    checkpoints = list((service.repository.base_dir / "tasks" / task.id / "checkpoints").glob("*.json"))
    assert any(
        json.loads(path.read_text(encoding="utf-8"))["kind"] == "runtime_context_compacted"
        for path in checkpoints
    )


def test_compactor_keeps_unfinished_tool_transaction_intact() -> None:
    model = BudgetModel([])
    counter = ThresholdCounter()
    pending = ModelMessage("assistant", "", actions=(ActionProposal("pending", "read.test"),))
    state = RuntimeState(
        context=ContextSnapshot("system"),
        messages=(ModelMessage("user", "current"), pending),
    )
    result = RuntimeCompactor(model=model, token_counter=counter).compact(
        state=state, request=_request(), action_schemas=(), target_tokens=600_000,
    )
    assert result.state.messages[-1] == pending


def test_input_compaction_keeps_typed_work_order_anchor() -> None:
    original = "x" * 1_100_000
    request = RunRequest(
        run_id="request:work", agent_id="chat", agent_version="1", channel="cli", user_input=original,
        protected_context=("节点验收条件：必须保留",),
    )
    result = RuntimeCompactor(model=BudgetModel([]), token_counter=ThresholdCounter(initial=1_000_000)).compact(
        state=RuntimeState(context=ContextSnapshot("system"), messages=(ModelMessage("user", original),)),
        request=request, action_schemas=(), target_tokens=600_000,
    )
    assert "节点验收条件：必须保留" in result.state.messages[0].content
