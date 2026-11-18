import json
import sys
import types
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harness.agents.chat import CHAT_AGENT
from harness.agents.registry import REGISTRY
from harness.channels import create_incoming_request
from harness.facade import Harness
from harness.interaction import ExecutionState, Interaction
from harness.runtime.agent_runtime import AgentRuntime, ChatInputs
from harness.runtime.task_intake import TaskIntake
from harness.tasks import (
    Artifact, Checkpoint, EffectReference, PlanNode, PlanPatch, PlanPatchOperation,
    TaskProposal, TaskRepository, TaskService, TaskSessionResolver, TaskTurnService,
)
from harness.orchestration import EffectIntent, ReviewVerdict, TaskOrchestrator
from harness.orchestration.models import WorkOrder, WorkOrderResult
from harness.tasks.session import TaskContext


def _proposal() -> TaskProposal:
    return TaskProposal(
        title="整理调研", objective="完成可验证的调研结论",
        acceptance_criteria=("给出来源",),
        nodes=(PlanNode("research", "调研", "收集事实", (), ("至少一个来源",)),),
        constraints=("不访问需要登录的来源",),
    )


def _service(tmp_path: Path) -> TaskService:
    return TaskService(TaskRepository(tmp_path / "task_store"))


def test_task_service_creates_versioned_plan_and_checkpoint(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = create_incoming_request(channel="cli", raw_text="复杂任务")

    task = service.create_task(_proposal(), request=request, agent=CHAT_AGENT)

    assert task.current_plan_version == 1
    assert task.origin_request_id == request.request_id
    assert task.constraints == ("不访问需要登录的来源",)
    assert service.repository.get_plan(task.id, 1).nodes[0].id == "research"
    links = service.repository.list_task_request_links(task.id)
    assert [(link.channel, link.request_id) for link in links] == [("cli", request.request_id)]
    email_request = create_incoming_request(channel="email", raw_text="继续", metadata={"continuation_key": "thread-1"})
    service.attach_request(task.id, email_request)
    assert len(service.repository.list_task_request_links(task.id)) == 2
    checkpoints = list((service.repository.base_dir / "tasks" / task.id / "checkpoints").glob("*.json"))
    assert len(checkpoints) == 1
    assert json.loads(checkpoints[0].read_text(encoding="utf-8"))["kind"] == "plan_changed"


def test_plan_patch_versions_and_rejects_stale_or_invalid_graph(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(_proposal(), request=create_incoming_request(channel="cli", raw_text="复杂任务"), agent=CHAT_AGENT)
    second = PlanNode("review", "复核", "检查结论", ("research",), ("覆盖验收条件",))
    plan = service.apply_patch(PlanPatch(task.id, 1, (PlanPatchOperation("add_node", node=second),)))

    assert plan.version == 2
    assert service.repository.get_task(task.id).current_plan_version == 2
    try:
        service.apply_patch(PlanPatch(task.id, 1, (PlanPatchOperation("add_node", node=second),)))
    except ValueError as exc:
        assert "过期" in str(exc)
    else:
        raise AssertionError("expected stale PlanPatch rejection")

    cyclic = PlanNode("research", "调研", "收集事实", ("review",), ("至少一个来源",))
    try:
        service.apply_patch(PlanPatch(task.id, 2, (PlanPatchOperation("update_node", node=cyclic),)))
    except ValueError as exc:
        assert "存在环" in str(exc)
    else:
        raise AssertionError("expected cyclic plan rejection")


def test_legacy_run_records_response_and_tool_artifacts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(_proposal(), request=create_incoming_request(channel="cli", raw_text="复杂任务"), agent=CHAT_AGENT)
    run = service.record_legacy_run(task, request_id="req_1", interaction={
        "ai_result": {"text": "已完成", "tool_calls": [{"name": "list_goals", "input": {}, "result": "[]"}]},
        "execution": {"success": True},
    })

    assert run.mode == "legacy_whole_task"
    assert run.retryable is False
    artifacts = list((service.repository.base_dir / "tasks" / task.id / "artifacts").rglob("*.json"))
    assert len(artifacts) == 2


def test_node_run_has_lifecycle_and_terminal_checkpoint(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(_proposal(), request=create_incoming_request(channel="cli", raw_text="复杂任务"), agent=CHAT_AGENT)

    run = service.start_node_run(task.id, node_id="research")
    finished = service.finish_node_run(task.id, run.id, status="succeeded", artifact_refs=("fact:v1",))

    assert run.status == "running" and run.ended_at is None
    assert finished.status == "succeeded" and finished.ended_at is not None
    assert service.repository.get_task(task.id).status == "in_progress"
    completed = service.transition_task(task.id, status="completed", evidence_refs=("fact:v1",))
    assert completed.status == "completed"


def test_repository_preserves_artifact_references_effects_and_checkpoints(tmp_path: Path) -> None:
    service = _service(tmp_path)
    task = service.create_task(_proposal(), request=create_incoming_request(channel="cli", raw_text="复杂任务"), agent=CHAT_AGENT)
    run = service.record_legacy_run(task, request_id="req_1", interaction={"ai_result": {}, "execution": {}})
    artifact = Artifact("artifact_binary", task.id, run.id, "image", "image/png", file_ref="/tmp/image.png")
    effect = EffectReference("effect_1", task.id, run.id, "send", "pending", "stable-key")
    checkpoint = Checkpoint("checkpoint_1", task.id, "tool_result", run_id=run.id)

    service.repository.save_artifact(artifact)
    service.repository.save_effect_reference(effect)
    service.repository.save_checkpoint(checkpoint)

    assert service.repository.get_artifact(task.id, artifact.id, version=1).file_ref == "/tmp/image.png"
    assert service.repository.get_effect_reference(task.id, effect.id).idempotency_key == "stable-key"
    assert service.repository.get_checkpoint(task.id, checkpoint.id).run_id == run.id


class _FakeProvider:
    def __init__(self, payload: dict):
        self.payload = payload

    def assess(self, _prompt: str) -> dict:
        return self.payload


def test_task_intake_returns_valid_proposal_but_invalid_model_falls_back(tmp_path: Path) -> None:
    repository = TaskRepository(tmp_path / "task_store")
    valid = {
        "kind": "create_task",
        "proposal": {
            "title": "整理调研", "objective": "完成调研", "acceptance_criteria": ["给出来源"],
            "nodes": [{"id": "research", "title": "调研", "description": "收集事实", "depends_on": [], "acceptance_criteria": ["至少一个来源"]}],
        },
    }
    request = create_incoming_request(channel="cli", raw_text="请调研并比较三个方案")
    decision = TaskIntake(_FakeProvider(valid)).assess(
        request, agent=CHAT_AGENT, task_summary="(none)", active_task_id=None,
    )
    assert decision.kind == "create_task"
    assert decision.proposal is not None

    fallback = TaskIntake(_FakeProvider({"kind": "create_task"})).assess(
        request, agent=CHAT_AGENT, task_summary="(none)", active_task_id=None,
    )
    assert fallback.kind == "ordinary"


def test_complex_cli_turn_is_internal_and_records_whole_run(tmp_path: Path) -> None:
    previous_scheduler = sys.modules.get("scheduler")
    sys.modules["scheduler"] = types.ModuleType("scheduler")
    try:
        from cli.dispatch import COMMAND_NAMES, dispatch
    finally:
        if previous_scheduler is None:
            del sys.modules["scheduler"]
        else:
            sys.modules["scheduler"] = previous_scheduler
    from harness.agents.registry import REGISTRY

    class Intake:
        def assess(self, *_args, **_kwargs):
            from harness.runtime.task_intake import TaskIntakeDecision
            return TaskIntakeDecision("create_task", proposal=_proposal())

    class Chat:
        def run(self, *_args, **_kwargs):
            return {
                "route": "ai", "command": None,
                "ai_result": {"text": "这是正常回复", "tool_calls": []},
                "execution": {"success": True, "executed": False, "kind": "agentic"},
            }

        def run_work_order(self, _request, *, work_order, input_artifacts, **_kwargs):
            return Interaction(
                route="work_order",
                text=f"{work_order.node.id} 完成，输入证据 {len(input_artifacts)} 条",
                execution=ExecutionState(
                    executed=True, kind="work_order", success=True, agent_id="chat",
                ),
            )

    class Inputs:
        def load(self, *_args, **_kwargs):
            return ChatInputs(goals=[], loops={})

    service = _service(tmp_path)
    runtime = AgentRuntime(chat_runtime=Chat(), task_intake=Intake(), chat_inputs_provider=Inputs())
    ctx = SimpleNamespace(
        channel_id="cli",
        harness=Harness(
            registry=REGISTRY,
            runtime=runtime,
            tasks=TaskTurnService(service, TaskSessionResolver(service.repository)),
        ),
    )
    result = dispatch(ctx, "请完成一项复杂调研")

    assert "research 完成，输入证据 0 条" in result.text
    assert "tasks" not in COMMAND_NAMES and "task" not in COMMAND_NAMES
    task_ids = list((service.repository.base_dir / "tasks").iterdir())
    assert len(task_ids) == 1
    task_dir = task_ids[0]
    assert len(list((task_dir / "runs").glob("*.json"))) == 1
    assert len(list((task_dir / "artifacts").rglob("*.json"))) == 1
    assert len(list((task_dir / "execution_plans").glob("*.json"))) == 1
    assert len(list((task_dir / "work_orders").glob("*.json"))) == 1


def test_harness_explicitly_orders_task_intent_execution_and_recording(tmp_path: Path) -> None:
    from harness.agents.registry import REGISTRY
    from harness.facade import Harness
    from harness.interaction import Interaction
    from harness.runtime.task_intake import TaskIntakeDecision

    events: list[str] = []

    class Runtime:
        def propose_task(self, *_args, **_kwargs):
            events.append("propose")
            return TaskIntakeDecision("create_task", proposal=_proposal())

        def execute_work_order(self, *_args, **_kwargs):
            events.append("work")
            return WorkOrderResult(success=True, text="done")

        def execute(self, *_args, **_kwargs):
            raise AssertionError("复杂 Task 不应走普通 Runtime")

    class Service(TaskService):
        def create_task(self, *args, **kwargs):
            events.append("create")
            return super().create_task(*args, **kwargs)

        def record_legacy_run(self, *args, **kwargs):
            events.append("record")
            return super().record_legacy_run(*args, **kwargs)

    service = Service(TaskRepository(tmp_path / "task_store"))
    result = Harness(
        registry=REGISTRY,
        runtime=Runtime(),  # type: ignore[arg-type]
        tasks=TaskTurnService(service, TaskSessionResolver(service.repository)),
    ).handle(
        channel="cli", raw_text="完成复杂调研",
    )

    assert "done" in result.text
    assert events == ["propose", "create", "work"]


def test_task_turn_rejects_stale_patch_without_new_checkpoint(tmp_path: Path) -> None:
    from harness.runtime.task_intake import TaskIntakeDecision

    service = _service(tmp_path)
    task = service.create_task(
        _proposal(),
        request=create_incoming_request(channel="cli", raw_text="复杂任务"),
        agent=CHAT_AGENT,
    )
    service.apply_patch(
        PlanPatch(
            task.id,
            1,
            (PlanPatchOperation("add_node", node=PlanNode("review", "复核", "检查", (), ("完整",))),),
        )
    )
    before = list((service.repository.base_dir / "tasks" / task.id / "checkpoints").glob("*.json"))
    turns = TaskTurnService(service, TaskSessionResolver(service.repository))
    stale = PlanPatch(
        task.id,
        1,
        (PlanPatchOperation("add_node", node=PlanNode("publish", "交付", "输出", (), ("可读",))),),
    )

    turn = turns.prepare_turn(
        request=create_incoming_request(channel="cli", raw_text="补充复核"),
        agent=CHAT_AGENT,
        assess_task_intent=lambda *_args, **_kwargs: TaskIntakeDecision("patch_active_task", patch=stale),
    )

    assert turn.terminal_interaction is not None
    assert turn.terminal_interaction.route == "task_rejected"
    assert len(list((service.repository.base_dir / "tasks" / task.id / "checkpoints").glob("*.json"))) == len(before)


def test_task_turn_service_does_not_depend_on_agent_runtime() -> None:
    turn_source = (Path(__file__).parent.parent / "harness" / "tasks" / "turns.py").read_text(encoding="utf-8")
    runtime_source = (Path(__file__).parent.parent / "harness" / "runtime" / "agent_runtime.py").read_text(encoding="utf-8")

    assert "AgentRuntime" not in turn_source
    assert "harness.tasks.repository" not in runtime_source
    assert "harness.tasks.service" not in runtime_source
    assert "harness.tasks.session" not in runtime_source


def test_orchestrator_runs_dag_with_direct_dependency_artifacts(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = create_incoming_request(channel="cli", raw_text="完成完整调研")
    proposal = TaskProposal(
        title="调研", objective="完成结论", acceptance_criteria=("可交付",),
        nodes=(
            PlanNode("research", "调研", "收集事实", (), ("给出事实",)),
            PlanNode("review", "整理", "基于事实得出结论", ("research",), ("给出结论",)),
        ),
    )
    task = service.create_task(proposal, request=request, agent=CHAT_AGENT)
    observed: list[tuple[str, int]] = []

    class Executor:
        def execute_work_order(self, _request, *, work_order, input_artifacts, **_kwargs):
            observed.append((work_order.node.id, len(input_artifacts)))
            return WorkOrderResult(success=True, text=f"{work_order.node.id} 的交付")

    result = TaskOrchestrator(task_service=service, registry=REGISTRY, executor=Executor()).orchestrate_or_resume(
        context=TaskContext(task=task, plan=service.repository.get_plan(task.id, 1)),
        request=request,
    )

    assert observed == [("research", 0), ("review", 1)]
    assert result is not None and "research 的交付" in result.text and "review 的交付" in result.text
    assert service.repository.get_task(task.id).status == "completed"


def test_orchestrator_pauses_and_resumes_failed_node(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = create_incoming_request(channel="cli", raw_text="完成调研")
    task = service.create_task(_proposal(), request=request, agent=CHAT_AGENT)

    class Executor:
        attempts = 0

        def execute_work_order(self, _request, **_kwargs):
            self.attempts += 1
            return WorkOrderResult(success=self.attempts > 1, text="恢复后的交付" if self.attempts > 1 else "", error="暂时失败")

    executor = Executor()
    orchestrator = TaskOrchestrator(task_service=service, registry=REGISTRY, executor=executor)
    first = orchestrator.orchestrate_or_resume(
        context=TaskContext(task=task, plan=service.repository.get_plan(task.id, 1)), request=request,
    )
    assert first is not None and first.route == "orchestration_paused"
    paused = service.repository.get_task(task.id)
    assert paused.status == "paused"

    second = orchestrator.orchestrate_or_resume(
        context=TaskContext(task=paused, plan=service.repository.get_plan(task.id, 1)),
        request=create_incoming_request(channel="cli", raw_text="继续"),
    )
    assert second is not None and second.route == "orchestrated"
    assert executor.attempts == 2
    assert service.repository.get_task(task.id).status == "completed"


def test_plan_patch_supersedes_execution_plan_and_carries_unchanged_evidence(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = create_incoming_request(channel="cli", raw_text="完成调研")
    proposal = TaskProposal(
        title="调研", objective="完成结论", acceptance_criteria=("可交付",),
        nodes=(
            PlanNode("research", "调研", "收集事实", (), ("给出事实",)),
            PlanNode("review", "整理", "形成结论", ("research",), ("给出结论",)),
        ),
    )
    task = service.create_task(proposal, request=request, agent=CHAT_AGENT)

    class Executor:
        calls: list[str] = []

        def execute_work_order(self, _request, *, work_order, **_kwargs):
            self.calls.append(work_order.node.id)
            if work_order.node.id == "review" and self.calls.count("review") == 1:
                return WorkOrderResult(success=False, error="需要补充结论结构")
            return WorkOrderResult(success=True, text=f"{work_order.node.id} 完成")

    executor = Executor()
    orchestrator = TaskOrchestrator(task_service=service, registry=REGISTRY, executor=executor)
    paused = orchestrator.orchestrate_or_resume(
        context=TaskContext(task=task, plan=service.repository.get_plan(task.id, 1)), request=request,
    )
    assert paused is not None and paused.route == "orchestration_paused"

    revised_review = PlanNode("review", "整理", "按新结构形成结论", ("research",), ("给出结论",))
    service.apply_patch(PlanPatch(task.id, 1, (PlanPatchOperation("update_node", node=revised_review),)))
    revised_task = service.repository.get_task(task.id)
    completed = orchestrator.orchestrate_or_resume(
        context=TaskContext(task=revised_task, plan=service.repository.get_plan(task.id, 2)),
        request=create_incoming_request(channel="cli", raw_text="按新结构继续"),
    )

    plans = orchestrator._repository.list_execution_plans(task.id)
    assert completed is not None and completed.route == "orchestrated"
    assert executor.calls == ["research", "review", "review"]
    assert any(plan.status == "superseded" for plan in plans)
    current = next(plan for plan in plans if plan.task_plan_version == 2)
    research = next(node for node in current.nodes if node.node.id == "research")
    assert research.status == "succeeded" and research.artifact_refs


def test_review_verdict_and_effect_intent_are_typed_and_self_review_is_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = create_incoming_request(channel="cli", raw_text="完成调研")
    task = service.create_task(_proposal(), request=request, agent=CHAT_AGENT)

    class Executor:
        def execute_work_order(self, _request, **_kwargs):
            return WorkOrderResult(success=True, text="交付")

    orchestrator = TaskOrchestrator(task_service=service, registry=REGISTRY, executor=Executor())
    orchestrator.orchestrate_or_resume(
        context=TaskContext(task=task, plan=service.repository.get_plan(task.id, 1)), request=request,
    )
    execution = orchestrator._repository.list_execution_plans(task.id)[0]
    order = orchestrator._repository.list_work_orders(task.id, execution_plan_id=execution.id)[0]
    artifact_ref = execution.nodes[0].artifact_refs[0]
    self_review = ReviewVerdict("verdict_self", task.id, order.id, "chat", (artifact_ref,), "approved", ("ok",))
    try:
        orchestrator.record_review_verdict(self_review)
    except ValueError as exc:
        assert "不能审批自己" in str(exc)
    else:
        raise AssertionError("expected self-review rejection")

    verdict = ReviewVerdict("verdict_other", task.id, order.id, "independent-reviewer", (artifact_ref,), "approved", ("证据充分",))
    intent = EffectIntent("intent_1", task.id, order.id, "publish", {"target": "draft"}, "publish:stable-key")
    orchestrator.record_review_verdict(verdict)
    orchestrator.record_effect_intent(intent)

    assert orchestrator._repository.get_review_verdict(task.id, verdict.id).reviewer_agent_id == "independent-reviewer"
    assert orchestrator._repository.get_effect_intent(task.id, intent.id).idempotency_key == "publish:stable-key"


def test_orchestrator_recovers_interrupted_work_order_without_replaying_effects(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = create_incoming_request(channel="cli", raw_text="完成调研")
    task = service.create_task(_proposal(), request=request, agent=CHAT_AGENT)

    class Executor:
        calls = 0

        def execute_work_order(self, _request, **_kwargs):
            self.calls += 1
            return WorkOrderResult(success=True, text="安全恢复后的交付")

    executor = Executor()
    orchestrator = TaskOrchestrator(task_service=service, registry=REGISTRY, executor=executor)
    execution = orchestrator._ensure_execution_plan(task, service.repository.get_plan(task.id, 1))
    run = service.start_node_run(task.id, node_id="research")
    order = WorkOrder(
        id="work_order_interrupted", execution_plan_id=execution.id, task_id=task.id,
        task_plan_version=1, node=execution.nodes[0].node, role="worker",
        agent_id="chat", agent_version=CHAT_AGENT.version, task_objective=task.objective,
        task_constraints=task.constraints, status="running", run_id=run.id,
    )
    orchestrator._repository.save_work_order(order)
    interrupted = replace(
        execution,
        nodes=(replace(execution.nodes[0], status="running", work_order_ids=(order.id,), attempts=1),),
    )
    orchestrator._repository.save_execution_plan(interrupted)

    result = orchestrator.orchestrate_or_resume(
        context=TaskContext(task=service.repository.get_task(task.id), plan=service.repository.get_plan(task.id, 1)),
        request=create_incoming_request(channel="cli", raw_text="继续"),
    )

    assert result is not None and result.route == "orchestrated"
    assert executor.calls == 1
    assert service.repository.get_run(task.id, run.id).status == "paused"
    assert service.repository.get_task(task.id).status == "completed"
