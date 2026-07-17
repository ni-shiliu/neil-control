"""Harness 的唯一渠道调用入口。"""

from __future__ import annotations

from typing import Any, Mapping

from harness.agents.registry import AgentRegistry, AgentRoute, AgentRoutingError, REGISTRY
from harness.channels import IncomingRequest, RequestIdentity, create_incoming_request
from harness.config import PersonalConfigRepository
from harness.memory import ConversationService, MemoryService
from harness.interaction import ExecutionState, Interaction
from harness.orchestration import TaskOrchestrator
from harness.runtime.agent_runtime import AgentRuntime
from harness.tasks import TaskRunJournal, TaskTurnService

class Harness:
    def __init__(
        self,
        *,
        registry: AgentRegistry,
        runtime: AgentRuntime,
        tasks: TaskTurnService | None = None,
        orchestrator: TaskOrchestrator | None = None,
        conversation: ConversationService | None = None,
        memory: MemoryService | None = None,
    ):
        self._registry = registry
        self._runtime = runtime
        self._tasks = tasks or TaskTurnService.build_default()
        self._conversation = conversation or ConversationService()
        self._memory = memory
        self._orchestrator = orchestrator or TaskOrchestrator(
            task_service=self._tasks.service,
            registry=registry,
            executor=runtime,
        )

    @classmethod
    def build_default(
        cls,
    ) -> "Harness":
        """组装通用 Harness 内核；不依赖 engine 的 Runtime、工具或 Context。"""
        from harness.capabilities import CapabilityExecutor, CapabilityRegistry
        from harness.agents.chat.capabilities import register_browser_actions
        from harness.governance import FanoutRunJournal, InMemoryRunJournal
        from harness.memory import MemoryRepository, MemoryService
        from harness.memory.capabilities import register_memory_actions
        from harness.memory.context import MemoryKnowledgeReader
        from harness.memory.projector import TaskMemoryProjector
        from harness.runtime.agent_runtime import HarnessAgentRuntime
        from harness.runtime.context import ContextAssembler
        from harness.runtime.model import AnthropicModelGateway
        from harness.runtime.runtime import Runtime

        conversation = ConversationService()
        memory = MemoryService(MemoryRepository())
        tasks = TaskTurnService.build_default(memory_projector=TaskMemoryProjector(memory))
        capabilities = CapabilityRegistry()
        register_browser_actions(capabilities)
        register_memory_actions(capabilities, memory)
        model = AnthropicModelGateway()
        return cls(
            registry=REGISTRY,
            runtime=HarnessAgentRuntime(
                runtime=Runtime(
                    model=model,
                    context=ContextAssembler(reader=MemoryKnowledgeReader(
                        memory=memory, conversation=conversation.repository,
                        personal_config=PersonalConfigRepository(),
                    )),
                ),
                model=model,
                capabilities=capabilities,
                executor=CapabilityExecutor(registry=capabilities),
                journal=FanoutRunJournal(InMemoryRunJournal(), TaskRunJournal(tasks.service)),
                environment_capabilities=frozenset({"browser"}),
            ),
            tasks=tasks,
            conversation=conversation,
            memory=memory,
        )

    def handle(
        self,
        *,
        channel: str,
        raw_text: str,
        metadata: Mapping[str, Any] | None = None,
        identity: RequestIdentity | Mapping[str, Any] | None = None,
    ) -> Interaction:
        """处理一条自然语言渠道输入。

        Facade 只负责按层编排，不包含 Agent、Task 或 Runtime 的领域规则：

        ``渠道输入 → ① Agent 路由 → ② Task 回合准备 → ③ 编排
        → ④ WorkOrder Runtime / 普通聊天 → ② 执行事实记录 → Interaction``。

        无论成功、Task 澄清/拒绝还是路由失败，均返回 ``Interaction``；
        不向渠道调用方抛出路由类异常，也不在此处输出 stdout。
        """

        # 渠道适配：将 CLI/Email/Telegram 等渠道的原始输入统一为瞬时请求对象。
        # metadata 只能承载由可信渠道适配器提供的附加信息，例如 task_id。
        request = self._adapt_channel(channel=channel, raw_text=raw_text, metadata=metadata, identity=identity)

        # ① Agent 层：根据 channel、默认 Agent 和可信 agent hint 路由请求。
        # 路由失败会被转换为 Interaction，因此 Facade 可以统一返回而不是抛异常。
        route = self._route_agent(request)
        if isinstance(route, Interaction):
            return self._finish(request, route)

        # ② Task 层（执行前）：
        # - 解析当前会话关联的 Task/Plan；
        # - 通过 Runtime 的“提案回调”让模型判断 ordinary/create/patch/clarify；
        # - 校验并持久化合法的 Task/Plan 变更。
        #
        # 这里传入的是方法引用，不是立刻执行模型调用。TaskTurnService 会在
        # prepare_turn() 内按其回调契约调用 Runtime，从而避免 Task 层依赖
        # AgentRuntime 这一具体实现。
        turn = self._tasks.prepare_turn(
            request=request,
            agent=route.agent,
            assess_task_intent=self._runtime.propose_task,
        )

        # Task 判断可能要求用户澄清，或拒绝无效/陈旧 PlanPatch。
        # 此时不应进入工具循环，直接把类型化结果交还渠道。
        if turn.terminal_interaction is not None:
            return self._finish(request, turn.terminal_interaction)

        # ③ 编排层：复杂/未完成 Task 进入 ExecutionPlan → WorkOrder → 节点 Run。
        # 没有 active Task 时返回 None，才会降到普通聊天 Runtime。
        orchestrated = self._orchestrator.orchestrate_or_resume(context=turn.context, request=request)
        if orchestrated is not None:
            return self._finish(request, orchestrated)

        # ④ Runtime 层：没有待编排 Task 的普通聊天回合。
        # Runtime 负责模型调用、工具循环和工具结果收集，但不写入 Task Repository。
        interaction = self._execute_runtime(request, route)

        # ② Task 层（兼容收尾）：普通聊天不创建 Task；若未来存在未由 L3 接管的
        # Task 回合，仍由这里记录 legacy_whole_task Run，保持旧调用方兼容。
        return self._finish(request, self._tasks.complete_turn(turn, request=request, interaction=interaction))

    @staticmethod
    def _adapt_channel(
        *,
        channel: str,
        raw_text: str,
        metadata: Mapping[str, Any] | None,
        identity: RequestIdentity | Mapping[str, Any] | None,
    ) -> IncomingRequest:
        """渠道适配层：只构造瞬时请求，不处理 Agent 或 Task。"""
        return create_incoming_request(
            channel=channel, raw_text=raw_text, identity=identity, metadata=dict(metadata or {}),
        )

    def _route_agent(self, request: IncomingRequest) -> AgentRoute | Interaction:
        """①层：选择并校验当前请求的 Agent，将路由错误转换为 Interaction。"""
        try:
            return self._registry.route(request)
        except AgentRoutingError as exc:
            return Interaction(
                route="agent_rejected", text=str(exc),
                execution=ExecutionState(executed=False, kind="agent_route", reason="rejected"),
            )

    def _execute_runtime(self, request: IncomingRequest, route: AgentRoute) -> Interaction:
        """④层：在已确定 Agent 下执行一次 Runtime 回合，不接触 Task 持久化。"""
        return self._runtime.execute(request, agent=route.agent)

    def _finish(self, request: IncomingRequest, interaction: Interaction) -> Interaction:
        """所有自然语言回合落为 Conversation；它不是长期记忆。"""
        try:
            self._conversation.record(request=request, interaction=interaction)
        except (OSError, TypeError, ValueError):
            # 会话记录写入不可反向破坏已经完成的渠道回复；可由观测层记录失败。
            pass
        return interaction

    def cleanup_retention(self) -> dict[str, int]:
        """供宿主启动或定时调用；不属于单个 Agent Run。"""
        if self._memory is None:
            return {"conversations": 0}
        return {
            "conversations": self._conversation.repository.cleanup(
                referenced_source_refs=self._memory.live_source_refs(), retention_days=30,
            ),
        }
