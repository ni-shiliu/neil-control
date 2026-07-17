"""① 层路由之后的通用 Agent 运行入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from harness.agents.definition import AgentDefinition
from harness.channels import IncomingRequest
from harness.interaction import ExecutionState, Interaction, ToolCallResult
from harness.orchestration.models import WorkOrder, WorkOrderResult
from harness.runtime.task_intake import TaskIntake, TaskIntakeDecision
from harness.runtime.task_intake import GatewayTaskAssessmentProvider
from harness.runtime.contracts import RunRequest, RunScope, RuntimeOutcome
from harness.runtime.runner import ControlledRun
from harness.runtime.runtime import Runtime
from harness.capabilities.executor import CapabilityExecutor
from harness.capabilities.registry import CapabilityRegistry
from harness.governance.governor import Governor
from harness.governance.journal import FanoutRunJournal, InMemoryRunJournal, NullRunJournal, RunJournal
from harness.runtime.model import ModelGateway
from harness.skills.registry import SKILL_REGISTRY
from harness.tasks.models import Artifact

if TYPE_CHECKING:
    from harness.runtime.chat_runtime import ChatRuntime

@dataclass(frozen=True)
class ChatInputs:
    goals: list[dict]
    loops: dict


class ChatInputsProvider(Protocol):
    """把宿主应用的可读上下文适配给聊天 Runtime。"""

    def load(self, request: IncomingRequest, agent: AgentDefinition) -> ChatInputs: ...


class ChatExecutor(Protocol):
    def run(self, request: IncomingRequest, *, agent: AgentDefinition, goals: list[dict], loops: dict) -> Interaction: ...
    def run_work_order(
        self,
        request: IncomingRequest,
        *,
        agent: AgentDefinition,
        work_order: WorkOrder,
        input_artifacts: tuple[Artifact, ...],
        goals: list[dict],
        loops: dict,
    ) -> Interaction: ...


class DefaultChatInputsProvider:
    """当前个人自动化宿主的上下文适配器；可由其他渠道/产品替换。"""

    def load(self, request: IncomingRequest, agent: AgentDefinition) -> ChatInputs:
        import goals
        from loops import discover

        return ChatInputs(goals=goals.list_all(), loops=discover())


class AgentRuntime:
    """④层 Runtime：提出结构化意图并执行已确定的 Agent 回合。"""

    def __init__(
        self,
        *,
        chat_runtime: ChatExecutor,
        task_intake: TaskIntake | None = None,
        chat_inputs_provider: ChatInputsProvider | None = None,
    ):
        self._chat_runtime = chat_runtime
        self._task_intake = task_intake or TaskIntake()
        self._chat_inputs_provider = chat_inputs_provider or DefaultChatInputsProvider()

    def propose_task(
        self,
        request: IncomingRequest,
        *,
        agent: AgentDefinition,
        task_summary: str,
        active_task_id: str | None,
    ) -> TaskIntakeDecision:
        """④层模型决策：仅提出 Task/Plan 意图，不持久化。"""
        return self._task_intake.assess(
            request, agent=agent, task_summary=task_summary, active_task_id=active_task_id,
        )

    def execute(self, request: IncomingRequest, *, agent: AgentDefinition) -> Interaction:
        """在既定 Agent 与上下文下执行一回合，不接触全局 Task。"""
        inputs = self._chat_inputs_provider.load(request, agent)
        return Interaction.coerce(
            self._chat_runtime.run(request, agent=agent, goals=inputs.goals, loops=inputs.loops)
        )

    def execute_work_order(
        self,
        request: IncomingRequest,
        *,
        agent: AgentDefinition,
        work_order: WorkOrder,
        input_artifacts: tuple[Artifact, ...],
    ) -> WorkOrderResult:
        """④层执行一个无工具 WorkOrder，仅返回候选 Artifact 内容。"""
        inputs = self._chat_inputs_provider.load(request, agent)
        interaction = Interaction.coerce(self._chat_runtime.run_work_order(
            request, agent=agent, work_order=work_order, input_artifacts=input_artifacts,
            goals=inputs.goals, loops=inputs.loops,
        ))
        if interaction.execution.success is False:
            return WorkOrderResult(success=False, error=interaction.text or "Worker Runtime 执行失败")
        return WorkOrderResult(success=True, text=interaction.text)


class HarnessAgentRuntime:
    """新的④层门面：普通请求与 WorkOrder 复用同一受控执行内核。"""

    def __init__(
        self,
        *,
        runtime: Runtime,
        model: ModelGateway,
        capabilities: CapabilityRegistry,
        executor: CapabilityExecutor,
        journal: RunJournal | None = None,
        environment_capabilities: frozenset[str] = frozenset(),
        task_intake: TaskIntake | None = None,
    ):
        self._runtime = runtime
        self._capabilities = capabilities
        self._executor = executor
        self._journal = journal or NullRunJournal()
        self._environment_capabilities = environment_capabilities
        self._task_intake = task_intake or TaskIntake(GatewayTaskAssessmentProvider(model))

    def propose_task(self, request: IncomingRequest, *, agent: AgentDefinition, task_summary: str, active_task_id: str | None) -> TaskIntakeDecision:
        return self._task_intake.assess(request, agent=agent, task_summary=task_summary, active_task_id=active_task_id)

    def execute(self, request: IncomingRequest, *, agent: AgentDefinition) -> Interaction:
        run_request = self._request_for(request=request, agent=agent)
        trace_journal = InMemoryRunJournal()
        outcome = self._run(agent=agent, request=run_request, trace_journal=trace_journal)
        return self._to_interaction(agent, outcome, decision_trace=self._decision_trace(trace_journal.events))

    def execute_work_order(
        self,
        request: IncomingRequest,
        *,
        agent: AgentDefinition,
        work_order: WorkOrder,
        input_artifacts: tuple[Artifact, ...],
    ) -> WorkOrderResult:
        evidence = "\n\n".join(str(item.content or item.file_ref or "") for item in input_artifacts)
        run_request = self._request_for(
            request=request,
            agent=agent,
            user_input=(
                f"完成 WorkOrder 节点：{work_order.node.title}\n{work_order.node.description}\n"
                f"验收条件：{'；'.join(work_order.node.acceptance_criteria)}\n"
                f"用户补充：{work_order.supplemental_input}\n直接依赖证据：{evidence or '(none)'}"
            ),
            scope=RunScope(kind="work_order", task_id=work_order.task_id, work_order_id=work_order.id, node_id=work_order.node.id),
            run_id=work_order.run_id or work_order.id,
            allowed_action_ids=frozenset(),
            protected_context=(
                f"节点目标：{work_order.node.title} — {work_order.node.description}",
                f"节点验收条件：{'；'.join(work_order.node.acceptance_criteria)}",
            ),
        )
        outcome = self._run(agent=agent, request=run_request)
        if outcome.status != "completed":
            return WorkOrderResult(success=False, error=outcome.reason or "WorkOrder 未完成")
        return WorkOrderResult(success=True, text=outcome.text)

    def _run(
        self,
        *,
        agent: AgentDefinition,
        request: RunRequest,
        trace_journal: InMemoryRunJournal | None = None,
    ) -> RuntimeOutcome:
        journal: RunJournal = self._journal if trace_journal is None else FanoutRunJournal(self._journal, trace_journal)
        return ControlledRun(
            runtime=self._runtime,
            governor=Governor(profile=agent.governance_profile, environment_capabilities=self._environment_capabilities),
            capabilities=self._capabilities,
            executor=self._executor,
            journal=journal,
        ).run(agent=agent, request=request, approvals=request.metadata)

    def _request_for(
        self,
        *,
        request: IncomingRequest,
        agent: AgentDefinition,
        user_input: str | None = None,
        scope: RunScope | None = None,
        run_id: str | None = None,
        allowed_action_ids: frozenset[str] | None = None,
        protected_context: tuple[str, ...] = (),
    ) -> RunRequest:
        allowed = allowed_action_ids
        if allowed is None:
            allowed = SKILL_REGISTRY.authorize(
                agent_id=agent.id,
                skill_ids=agent.skill_grants,
                available_tool_names=self._capabilities.action_ids(),
            ).action_ids
        return RunRequest(
            run_id=run_id or f"request:{request.request_id}", agent_id=agent.id,
            agent_version=agent.version, channel=request.channel,
            user_input=user_input if user_input is not None else request.raw_text,
            identity=request.identity, scope=scope or RunScope(), metadata=dict(request.metadata), allowed_action_ids=allowed,
            memory_write_scopes=agent.knowledge_policy.write_scopes,
            protected_context=protected_context,
        )

    @staticmethod
    def _decision_trace(events: list[tuple[str, dict]]) -> tuple[dict, ...]:
        """面向后续 CLI 的可审计决策轨迹，不记录或暴露私有思维链。"""
        trace: list[dict] = []
        for kind, metadata in events:
            if kind == "run_started":
                trace.append({"stage": "run_started", "scope": metadata.get("scope", "conversation")})
            elif kind == "model_decision":
                trace.append({
                    "stage": "model_decision",
                    "iteration": metadata.get("iteration"),
                    "decision": metadata.get("kind"),
                    "actions": list(metadata.get("action_ids", ())),
                })
            elif kind == "action_decision":
                trace.append({
                    "stage": "governance",
                    "decision": metadata.get("kind"),
                    "reason": metadata.get("reason", ""),
                    "actions": list(metadata.get("action_ids", ())),
                })
            elif kind == "observations_recorded":
                trace.append({
                    "stage": "action_results",
                    "success": bool(metadata.get("success", False)),
                    "actions": list(metadata.get("actions", ())),
                })
        return tuple(trace)

    @staticmethod
    def _to_interaction(
        agent: AgentDefinition,
        outcome: RuntimeOutcome,
        *,
        decision_trace: tuple[dict, ...] = (),
    ) -> Interaction:
        calls = tuple(ToolCallResult(
            name=item.action_id, input=item.input, result=item.content,
        ) for item in outcome.observations)
        if outcome.status == "completed":
            return Interaction(
                route="ai", text=outcome.text, tool_calls=calls,
                payload={"decision_trace": decision_trace},
                execution=ExecutionState(
                    executed=bool(calls), success=True, kind="controlled_run", agent_id=agent.id,
                    tool_names=tuple(item.action_id for item in outcome.observations),
                ),
            )
        return Interaction(
            route="interrupted" if outcome.status == "interrupted" else "agent_rejected",
            text=outcome.reason or "Run 未完成",
            tool_calls=calls,
            payload={"decision_trace": decision_trace},
            execution=ExecutionState(
                executed=bool(calls), success=False, kind="controlled_run", reason=outcome.status,
                agent_id=agent.id, tool_names=tuple(item.action_id for item in outcome.observations),
            ),
        )
