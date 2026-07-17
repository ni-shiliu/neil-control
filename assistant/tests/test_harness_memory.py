from __future__ import annotations

from pathlib import Path

from harness.agents.chat.definition import CHAT_AGENT
from harness.capabilities.models import CapabilityRequest
from harness.channels import RequestIdentity, create_incoming_request
from harness.config import PersonalConfigRepository
from harness.interaction import Interaction
from harness.memory import ConversationRepository, ConversationService, MemoryRepository, MemoryService
from harness.memory.context import MemoryKnowledgeReader
from harness.memory.capabilities import MemoryProposalHandler
from harness.memory.projector import TaskMemoryProjector
from harness.runtime.context import ContextAssembler
from harness.runtime.contracts import ActionProposal, ModelResponse, RunRequest
from harness.governance import AuthorizedAction


def _identity(*, project_id: str | None = None) -> RequestIdentity:
    return RequestIdentity(tenant_id="tenant", user_id="user", thread_id="thread", project_id=project_id)


def test_conversation_is_partitioned_by_thread_and_day(tmp_path: Path) -> None:
    conversation = ConversationService(ConversationRepository(tmp_path / "conversation"))
    request = create_incoming_request(channel="cli", raw_text="hello", identity=_identity())
    record = conversation.record(request=request, interaction=Interaction(route="ai", text="world"))
    assert record is not None
    path = tmp_path / "conversation" / "tenants" / "tenant" / "users" / "user" / f"{request.created_at[:10]}_001.jsonl"
    assert path.exists()
    assert path.read_text(encoding="utf-8").startswith('{"created_at":')
    assert record.source_ref == f"conversation:{request.request_id}"


def test_explicit_user_preference_is_written_to_one_markdown_document(tmp_path: Path) -> None:
    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    candidate = memory.create_candidate(
        scope="user", tenant_id="tenant", owner_id="user", kind="preference", semantic_key="response.language",
        content={"value": "zh-CN"}, source_ref="conversation:turn_1",
        write_policy="explicit_preference_auto",
    )
    record = memory.promote(candidate)
    assert record.version == 1
    assert memory.list_current("user", "tenant", "user")[0].content["value"] == "zh-CN"
    assert (tmp_path / "memory" / "user" / "tenant" / "user.md").exists()
    assert not (tmp_path / "memory" / "records").exists()


def test_low_sensitivity_explicit_name_fact_promotes(tmp_path: Path) -> None:
    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    record = memory.promote(memory.create_candidate(
        scope="user", tenant_id="tenant", owner_id="user", kind="fact", semantic_key="user.name",
        content={"value": "Nishiliu"}, source_ref="conversation:turn_2", sensitivity="low",
        write_policy="explicit_user_memory_auto",
    ))
    assert record.content == {"value": "Nishiliu"}


def test_name_correction_uses_current_turn_even_if_model_supplies_old_source_ref(tmp_path: Path) -> None:
    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    proposal = ActionProposal(
        "memory-call", "memory_propose", {
            "scope": "user", "kind": "fact", "key": "user.name", "content": {"value": "Nishiliu"},
            "source_ref": "conversation:previous_turn", "sensitivity": "low",
            "write_policy": "explicit_user_memory_auto",
        },
    )
    result = MemoryProposalHandler(memory).execute(CapabilityRequest(
        run=RunRequest(
            run_id="request:current_turn", agent_id="chat", agent_version="1", channel="cli",
            user_input="刚刚是猫发的，其实我是 Nishiliu", identity=_identity(),
            memory_write_scopes=frozenset({"memory.user"}),
        ),
        action=AuthorizedAction(proposal, "key"),
    ))
    assert result.success is True
    assert memory.list_current("user", "tenant", "user")[0].content == {"value": "Nishiliu"}


def test_model_selected_sensitivity_does_not_decide_promotion(tmp_path: Path) -> None:
    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    proposal = ActionProposal(
        "memory-call", "memory_propose", {
            "scope": "user", "kind": "fact", "key": "user.secret", "content": {"value": "x"},
            "sensitivity": "high", "write_policy": "explicit_user_memory_auto",
        },
    )
    result = MemoryProposalHandler(memory).execute(CapabilityRequest(
        run=RunRequest(
            run_id="request:turn_3", agent_id="chat", agent_version="1", channel="cli",
            user_input="记住", identity=_identity(), memory_write_scopes=frozenset({"memory.user"}),
        ),
        action=AuthorizedAction(proposal, "key"),
    ))
    assert result.success is True
    assert memory.list_current("user", "tenant", "user")[0].content == {"value": "x"}
    assert not (tmp_path / "memory" / "candidates").exists()


def test_invalid_memory_proposal_is_marked_rejected(tmp_path: Path) -> None:
    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    proposal = ActionProposal(
        "memory-call", "memory_propose", {
            "scope": "user", "kind": "fact", "key": "user.secret", "content": {"value": "x"},
            "sensitivity": "private", "write_policy": "explicit_user_memory_auto",
        },
    )
    result = MemoryProposalHandler(memory).execute(CapabilityRequest(
        run=RunRequest(
            run_id="request:turn_3", agent_id="chat", agent_version="1", channel="cli",
            user_input="记住", identity=_identity(), memory_write_scopes=frozenset({"memory.user"}),
        ),
        action=AuthorizedAction(proposal, "key"),
    ))
    assert result.success is False
    assert memory.list_current("user", "tenant", "user") == []
    assert not (tmp_path / "memory" / "candidates").exists()


def test_user_memory_markdown_is_editable_and_overrides_display_value(tmp_path: Path) -> None:
    repository = MemoryRepository(tmp_path / "memory")
    memory = MemoryService(repository)
    memory.promote(memory.create_candidate(
        scope="user", tenant_id="tenant", owner_id="user", kind="fact", semantic_key="user.name",
        content={"value": "Neil"}, source_ref="conversation:turn_1",
        write_policy="explicit_user_memory_auto",
    ))
    document = tmp_path / "memory" / "user" / "tenant" / "user.md"
    assert "| user.name | fact | Neil |" in document.read_text(encoding="utf-8")
    document.write_text(
        "# 用户记忆\n\n| key | kind | value |\n| --- | --- | --- |\n| user.name | fact | NEil |\n",
        encoding="utf-8",
    )
    assert memory.list_current("user", "tenant", "user")[0].content == {"value": "NEil"}


def test_conversation_is_date_partitioned_and_thread_is_read_filter(tmp_path: Path) -> None:
    conversation = ConversationService(ConversationRepository(tmp_path / "conversation"))
    first = create_incoming_request(channel="cli", raw_text="first", identity=_identity())
    other_identity = RequestIdentity(tenant_id="tenant", user_id="user", thread_id="other")
    second = create_incoming_request(channel="cli", raw_text="other", identity=other_identity)
    conversation.record(request=first, interaction=Interaction(route="ai", text="one"))
    conversation.record(request=second, interaction=Interaction(route="ai", text="two"))
    records = conversation.repository.list_recent(tenant_id="tenant", user_id="user", thread_id="thread")
    assert [record.raw_text for record in records] == ["first"]
    assert len(list((tmp_path / "conversation" / "tenants" / "tenant" / "users" / "user").glob("*.jsonl"))) == 1


def test_conversation_keeps_a_safe_decision_trace(tmp_path: Path) -> None:
    conversation = ConversationService(ConversationRepository(tmp_path / "conversation"))
    request = create_incoming_request(channel="cli", raw_text="我是 Neil", identity=_identity())
    record = conversation.record(
        request=request,
        interaction=Interaction(
            route="ai", text="你好，Neil。",
            payload={"decision_trace": ({"stage": "action_proposed", "actions": ({"id": "memory_propose"},)},)},
        ),
    )
    assert record is not None
    assert record.decision_trace[0]["stage"] == "action_proposed"
    restored = conversation.repository.list_recent(tenant_id="tenant", user_id="user", thread_id="thread")
    assert restored[0].decision_trace == record.decision_trace


def test_memory_decision_is_delegated_to_agent_but_hides_internal_status() -> None:
    context = ContextAssembler().assemble(
        agent=CHAT_AGENT,
        request=RunRequest(
            run_id="request:turn", agent_id="chat", agent_version="1", channel="cli",
            user_input="我是 Neil", identity=_identity(), memory_write_scopes=frozenset({"memory.user"}),
        ),
    )
    assert "完全由你依据当前语义" in context.system_prompt
    assert "不要在对用户的回复中回显" in context.system_prompt
    assert "Runtime 不按句式自动创建记忆" in context.system_prompt


def test_agent_policy_selects_conversation_user_memory_and_personal_config(tmp_path: Path) -> None:
    conversation = ConversationService(ConversationRepository(tmp_path / "conversation"))
    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    config = PersonalConfigRepository(tmp_path / "config")
    request = create_incoming_request(channel="cli", raw_text="remember Chinese", identity=_identity())
    conversation.record(request=request, interaction=Interaction(route="ai", text="done"))
    memory.promote(memory.create_candidate(
        scope="user", tenant_id="tenant", owner_id="user", kind="preference", semantic_key="language",
        content={"value": "zh-CN"}, source_ref=f"conversation:{request.request_id}",
        write_policy="explicit_preference_auto",
    ))
    config.save(tenant_id="tenant", user_id="user", config={"timezone": "Asia/Shanghai"})
    reader = MemoryKnowledgeReader(memory=memory, conversation=conversation.repository, personal_config=config)
    entries = reader.read(
        agent=CHAT_AGENT,
        request=RunRequest(
            run_id="request:test", agent_id="chat", agent_version="1", channel="cli",
            user_input="hello", identity=_identity(),
        ),
    )
    text = "\n".join(value for _, value in entries)
    assert "zh-CN" in text
    assert "remember Chinese" in text
    assert "Asia/Shanghai" in text


def test_cli_chat_agent_does_not_read_project_memory(tmp_path: Path) -> None:
    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    project_id = "project-1"
    memory.promote(memory.create_candidate(
        scope="project", tenant_id="tenant", owner_id=project_id, kind="fact", semantic_key="secret",
        content={"value": "not for cli chat"}, source_ref="task_plan:task-1:v1",
        write_policy="evidence_required",
    ))
    reader = MemoryKnowledgeReader(
        memory=memory, conversation=ConversationRepository(tmp_path / "conversation"),
        personal_config=PersonalConfigRepository(tmp_path / "config"),
    )
    entries = reader.read(
        agent=CHAT_AGENT,
        request=RunRequest(
            run_id="request:test", agent_id="chat", agent_version="1", channel="cli",
            user_input="hello", identity=_identity(project_id=project_id),
        ),
    )
    assert "not for cli chat" not in "\n".join(value for _, value in entries)


def test_project_memory_uses_one_markdown_document(tmp_path: Path) -> None:
    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    memory.promote(memory.create_candidate(
        scope="project", tenant_id="tenant", owner_id="project-1", kind="decision", semantic_key="api.choice",
        content={"value": "REST"}, source_ref="task_plan:task-1:v1", write_policy="evidence_required",
    ))
    memory.promote(memory.create_candidate(
        scope="project", tenant_id="tenant", owner_id="project-1", kind="decision", semantic_key="api.choice",
        content={"value": "GraphQL"}, source_ref="task_plan:task-1:v2", write_policy="evidence_required",
    ))
    document = tmp_path / "memory" / "project" / "tenant" / "project-1.md"
    assert document.exists()
    assert document.read_text(encoding="utf-8").count("api.choice") == 1
    assert memory.list_current("project", "tenant", "project-1")[0].content == {"value": "GraphQL"}


def test_task_projector_only_creates_project_memory_when_project_exists(tmp_path: Path) -> None:
    from harness.tasks.models import PlanNode, Task, TaskPlan

    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    projector = TaskMemoryProjector(memory)
    task = Task(
        id="task-1", title="title", objective="objective", acceptance_criteria=("done",),
        agent_id="chat", agent_version="1", workflow_id="chat", workflow_version="1",
        origin_channel="cli", origin_request_id="request-1", tenant_id="tenant",
    )
    plan = TaskPlan(
        task_id=task.id, version=1, acceptance_criteria=("done",),
        nodes=(PlanNode(id="node", title="node", description="work", acceptance_criteria=("done",)),),
    )
    projector.plan_changed(task, plan)
    assert memory.list_current("project", "tenant", "anything") == []


def test_controlled_run_promotes_preference_and_records_conversation(tmp_path: Path) -> None:
    from harness.agents.registry import AgentRegistry
    from harness.capabilities import CapabilityExecutor, CapabilityRegistry
    from harness.facade import Harness
    from harness.memory.capabilities import register_memory_actions
    from harness.runtime.agent_runtime import HarnessAgentRuntime
    from harness.runtime.context import ContextAssembler
    from harness.runtime.runtime import Runtime
    from harness.tasks import TaskRepository, TaskService, TaskSessionResolver, TaskTurnService

    class FakeGateway:
        def __init__(self):
            self.calls = 0

        def complete_json(self, **_kwargs):
            return {"kind": "ordinary"}

        def complete(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return ModelResponse(actions=(ActionProposal(
                    call_id="memory-1", action_id="memory_propose",
                    input={
                        "scope": "user", "kind": "preference", "key": "response.language",
                        "content": {"value": "zh-CN"}, "sensitivity": "low",
                        "write_policy": "explicit_preference_auto",
                    },
                ),))
            return ModelResponse(text="已记住。")

    conversation = ConversationService(ConversationRepository(tmp_path / "conversation"))
    memory = MemoryService(MemoryRepository(tmp_path / "memory"))
    config = PersonalConfigRepository(tmp_path / "config")
    capabilities = CapabilityRegistry()
    register_memory_actions(capabilities, memory)
    gateway = FakeGateway()
    runtime = HarnessAgentRuntime(
        runtime=Runtime(model=gateway, context=ContextAssembler(reader=MemoryKnowledgeReader(
            memory=memory, conversation=conversation.repository, personal_config=config,
        ))),
        model=gateway, capabilities=capabilities, executor=CapabilityExecutor(registry=capabilities),
    )
    repository = TaskRepository(tmp_path / "tasks")
    tasks = TaskTurnService(TaskService(repository), TaskSessionResolver(repository))
    registry = AgentRegistry((CHAT_AGENT,), default_agents_by_channel={"cli": "chat"})
    harness = Harness(registry=registry, runtime=runtime, tasks=tasks, conversation=conversation, memory=memory)
    interaction = harness.handle(channel="cli", raw_text="以后都用中文，记住", identity=_identity())
    assert interaction.text == "已记住。"
    assert memory.list_current("user", "tenant", "user")[0].content == {"value": "zh-CN"}
    assert conversation.repository.list_recent(tenant_id="tenant", user_id="user", thread_id="thread")[0].response_text == "已记住。"
