"""兼容入口：聊天型 Agent 复用新的④–⑥受控运行内核。"""

from __future__ import annotations

from harness.agents.definition import AgentDefinition
from harness.capabilities import CapabilityExecutor, CapabilityRegistry
from harness.channels import IncomingRequest
from harness.interaction import ExecutionState, Interaction
from harness.runtime.agent_runtime import HarnessAgentRuntime
from harness.runtime.context import ContextAssembler
from harness.runtime.model import AnthropicModelGateway
from harness.runtime.runtime import Runtime
from harness.orchestration.models import WorkOrder
from harness.tasks.models import Artifact


class ChatRuntime:
    """保留旧调用签名，不再拥有独立模型/工具循环。"""

    def __init__(self, *, runtime: HarnessAgentRuntime | None = None, **_ignored):
        if runtime is None:
            model = AnthropicModelGateway()
            capabilities = CapabilityRegistry()
            runtime = HarnessAgentRuntime(
                runtime=Runtime(model=model, context=ContextAssembler()),
                model=model,
                capabilities=capabilities,
                executor=CapabilityExecutor(registry=capabilities),
            )
        self._runtime = runtime

    def run(self, request: IncomingRequest, *, agent: AgentDefinition, goals: list[dict] | None = None, loops: dict | None = None) -> Interaction:
        return self._runtime.execute(request, agent=agent)

    def run_work_order(
        self,
        request: IncomingRequest,
        *,
        agent: AgentDefinition,
        work_order: WorkOrder,
        input_artifacts: tuple[Artifact, ...],
        goals: list[dict] | None = None,
        loops: dict | None = None,
    ) -> Interaction:
        result = self._runtime.execute_work_order(
            request, agent=agent, work_order=work_order, input_artifacts=input_artifacts,
        )
        return Interaction(
            route="work_order", text=result.text or result.error or "",
            execution=ExecutionState(
                executed=result.success, success=result.success, kind="work_order", agent_id=agent.id,
            ),
        )
