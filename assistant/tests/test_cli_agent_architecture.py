import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harness.agents.chat import CHAT_AGENT
from harness.agents.chat.definition import CHAT_IDENTITY
from harness.agents.chat.definition import CHAT_KNOWLEDGE_POLICY
from harness.agents.chat.definition import CHAT_WORKFLOW
from harness.agents.definition import AgentDefinition
from harness.agents.registry import AgentRegistry, AgentRegistryError, AgentRoutingError, REGISTRY
from harness.channels import create_incoming_request
from harness.channels.request import IncomingRequest
from harness.facade import Harness
from harness.interaction import ExecutionState, Interaction
from harness.capabilities import CapabilityExecutor, CapabilityRegistry
from harness.runtime import ContextAssembler, ModelResponse, Runtime
from harness.runtime.agent_runtime import HarnessAgentRuntime
from harness.skills.registry import SKILL_REGISTRY


def test_cli_request_uses_channel_default_agent_and_preserves_text() -> None:
    default_route = REGISTRY.route(create_incoming_request(channel="cli", raw_text="查看当前目标"))
    at_text_route = REGISTRY.route(create_incoming_request(channel="cli", raw_text="@chat 查看当前目标"))

    assert default_route.agent.id == "chat"
    assert default_route.request.raw_text == "查看当前目标"
    assert at_text_route.agent.id == "chat"
    assert at_text_route.request.raw_text == "@chat 查看当前目标"


def test_registry_accepts_trusted_explicit_agent_and_rejects_unknown_one() -> None:
    request = create_incoming_request(channel="cli", raw_text="查看当前目标")

    assert REGISTRY.route(request, agent_id="chat").agent.id == "chat"
    try:
        REGISTRY.route(request, agent_id="unknown")
    except AgentRoutingError as exc:
        assert "未知 Agent" in str(exc)
    else:
        raise AssertionError("expected routing failure")


def test_registry_rejects_duplicate_unknown_skill_and_wrong_channel() -> None:
    duplicate = AgentRegistry((CHAT_AGENT,), default_agents_by_channel={"cli": "chat"})
    assert duplicate.get("chat") == CHAT_AGENT

    try:
        AgentRegistry((CHAT_AGENT, CHAT_AGENT), default_agents_by_channel={"cli": "chat"})
    except AgentRegistryError as exc:
        assert "重复" in str(exc)
    else:
        raise AssertionError("expected duplicate Agent rejection")

    invalid_skill_agent = AgentDefinition(
        id="invalid-skill",
        version="1",
        identity=CHAT_IDENTITY,
        workflow_template=CHAT_WORKFLOW,
        knowledge_policy=CHAT_KNOWLEDGE_POLICY,
        skill_grants=frozenset({"missing.skill"}),
        allowed_channels=frozenset({"cli"}),
    )
    try:
        AgentRegistry((invalid_skill_agent,), default_agents_by_channel={"cli": "invalid-skill"})
    except ValueError as exc:
        assert "未知 Skill" in str(exc)
    else:
        raise AssertionError("expected unknown Skill rejection")

    non_cli_agent = AgentDefinition(
        id="scheduler-only",
        version="1",
        identity=CHAT_IDENTITY,
        workflow_template=CHAT_WORKFLOW,
        knowledge_policy=CHAT_KNOWLEDGE_POLICY,
        skill_grants=CHAT_AGENT.skill_grants,
        allowed_channels=frozenset({"scheduler"}),
    )
    registry = AgentRegistry((non_cli_agent,), default_agents_by_channel={"cli": "scheduler-only"})
    try:
        registry.route(IncomingRequest(channel="cli", raw_text="hello"))
    except AgentRoutingError as exc:
        assert "不支持 cli" in str(exc)
    else:
        raise AssertionError("expected channel rejection")


def test_request_factory_requires_a_dynamic_channel() -> None:
    request = create_incoming_request(channel="telegram", raw_text="hello")

    assert request.channel == "telegram"
    try:
        create_incoming_request(channel=" ", raw_text="hello")
    except ValueError as exc:
        assert "channel" in str(exc)
    else:
        raise AssertionError("expected empty channel rejection")


def test_chat_identity_is_loaded_from_markdown() -> None:
    identity_path = Path(__file__).parent.parent / "harness" / "agents" / "chat" / "identity.md"

    assert identity_path.exists()
    assert CHAT_IDENTITY.role == "个人自动化助手的管理界面"
    assert "工作原则：" in CHAT_IDENTITY.working_principles


def test_chat_agent_declares_a_valid_default_workflow() -> None:
    workflow_path = Path(__file__).parent.parent / "harness" / "agents" / "chat" / "workflow.md"

    assert workflow_path.exists()
    assert CHAT_AGENT.workflow_template == CHAT_WORKFLOW
    assert [step.id for step in CHAT_WORKFLOW.steps] == ["understand", "act", "respond"]


def test_chat_agent_declares_and_loads_knowledge_policy() -> None:
    knowledge_path = Path(__file__).parent.parent / "harness" / "agents" / "chat" / "knowledge.md"

    assert knowledge_path.exists()
    assert CHAT_AGENT.knowledge_policy == CHAT_KNOWLEDGE_POLICY
    assert "memory.user" in CHAT_KNOWLEDGE_POLICY.read_scopes
    assert "memory.user" in CHAT_KNOWLEDGE_POLICY.write_scopes


def test_chat_skills_resolve_to_registered_action_surface() -> None:
    all_action_ids = {
        "list_goals", "show_goal", "pause_goal", "resume_goal", "delete_goal", "rerun_goal", "create_goal",
        "update_goal_preferences", "update_loop_preferences", "update_user_preferences",
        "browser_open_url", "browser_observe", "browser_wait", "browser_click_text", "browser_type", "browser_diagnostic",
    }
    authorized = SKILL_REGISTRY.authorize(
        agent_id=CHAT_AGENT.id,
        skill_ids=CHAT_AGENT.skill_grants,
        available_tool_names=all_action_ids,
    )
    assert authorized.action_ids == all_action_ids


def test_cli_builtin_commands_stay_ahead_of_agent_routing() -> None:
    from cli import commands
    from cli.dispatch import dispatch

    calls: list[str] = []
    original = commands.cmd_list
    commands.cmd_list = lambda: calls.append("list")
    try:
        result = dispatch(None, "list")  # type: ignore[arg-type]
    finally:
        commands.cmd_list = original

    assert calls == ["list"]
    assert result.route == "command"


def test_cli_help_hides_internal_agent_routing() -> None:
    from cli.dispatch import render_help

    assert "@chat" not in render_help()


def test_harness_handle_hides_request_routing_and_returns_interaction() -> None:
    class Runtime:
        def __init__(self):
            self.request = None
            self.agent = None

        def propose_task(self, request, *, agent, task_summary, active_task_id):
            from harness.runtime.task_intake import TaskIntakeDecision
            return TaskIntakeDecision("ordinary")

        def execute(self, request, *, agent):
            self.request = request
            self.agent = agent
            return Interaction(route="ai", text="ok", execution=ExecutionState(kind="agentic"))

    runtime = Runtime()
    harness = Harness(registry=REGISTRY, runtime=runtime)  # type: ignore[arg-type]
    result = harness.handle(channel="cli", raw_text="hello", metadata={"source": "test"})

    assert result.text == "ok"
    assert runtime.request.channel == "cli"
    assert runtime.request.metadata == {"source": "test"}
    assert runtime.agent.id == "chat"

    rejected = Harness(
        registry=AgentRegistry((CHAT_AGENT,), default_agents_by_channel={}),
        runtime=runtime,  # type: ignore[arg-type]
    ).handle(channel="email", raw_text="hello")
    assert rejected.route == "agent_rejected"
    assert rejected.execution.reason == "rejected"


def test_facade_only_coordinates_task_turn_service() -> None:
    from pathlib import Path

    source = (Path(__file__).parent.parent / "harness" / "facade.py").read_text(encoding="utf-8")

    assert "_resolve_task_context" not in source
    assert "_apply_task_intent" not in source
    assert "_record_task_run" not in source
    assert "self._tasks.prepare_turn(" in source
    assert "self._tasks.complete_turn(" in source


def test_work_order_runtime_has_no_tool_schema() -> None:
    from harness.runtime.chat_runtime import ChatRuntime
    from harness.orchestration.models import WorkOrder
    from harness.tasks import PlanNode

    class Model:
        def complete(self, **_kwargs):
            return ModelResponse(text="节点交付")

        def complete_json(self, **_kwargs):
            return {"kind": "ordinary"}

    model = Model()
    registry = CapabilityRegistry()
    runtime = HarnessAgentRuntime(
        runtime=Runtime(model=model, context=ContextAssembler()), model=model,
        capabilities=registry, executor=CapabilityExecutor(registry=registry),
    )
    result = ChatRuntime(runtime=runtime).run_work_order(
        create_incoming_request(channel="cli", raw_text="执行"),
        agent=CHAT_AGENT,
        work_order=WorkOrder(
            id="work_order_test", execution_plan_id="execution_plan_test", task_id="task_test",
            task_plan_version=1, node=PlanNode("node", "节点", "完成节点", (), ("可验收",)),
            role="worker", agent_id="chat", agent_version=CHAT_AGENT.version,
            task_objective="完成任务", task_constraints=(),
        ),
        input_artifacts=(), goals=[], loops={},
    )

    assert result.execution.success is True
    assert result.text == "节点交付"
