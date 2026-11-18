"""④ 层单 Agent Runtime：模型调用、状态推进与结构化决策。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from harness.agents.definition import AgentDefinition
from harness.runtime.context import ContextAssembler
from harness.runtime.compaction import CompactionResult, RuntimeCompactor
from harness.runtime.contracts import (
    ContextUsage, ModelMessage, Observation, RunRequest, RuntimeDecision, RuntimeState,
)
from harness.runtime.model import ModelGateway
from harness.runtime.token_count import GatewayTokenCounter, TokenCounter


class Runtime:
    def __init__(
        self,
        *,
        model: ModelGateway,
        context: ContextAssembler,
        token_counter: TokenCounter | None = None,
        compactor: RuntimeCompactor | None = None,
    ):
        self._model = model
        self._context = context
        self._token_counter = token_counter or GatewayTokenCounter(model)
        self._compactor = compactor or RuntimeCompactor(model=model, token_counter=self._token_counter)

    def start(self, *, agent: AgentDefinition, request: RunRequest) -> RuntimeState:
        return RuntimeState(
            context=self._context.assemble(agent=agent, request=request),
            messages=(ModelMessage("user", request.user_input),),
        )

    def decide(
        self,
        *,
        state: RuntimeState,
        action_schemas: Sequence[Mapping[str, object]],
    ) -> tuple[RuntimeDecision, RuntimeState]:
        try:
            response = self._model.complete(
                system_prompt=state.context.system_prompt,
                messages=state.messages,
                action_schemas=action_schemas,
            )
        except Exception as exc:
            return RuntimeDecision(kind="error", error=str(exc)), state
        next_state = RuntimeState(
            context=state.context,
            messages=state.messages + (ModelMessage("assistant", response.text, actions=response.actions),),
            iteration=state.iteration + 1,
            observations=state.observations,
            run_summary=state.run_summary,
            original_input_ref=state.original_input_ref,
        )
        if response.actions:
            return RuntimeDecision(kind="actions", text=response.text, actions=response.actions), next_state
        if response.stop_reason not in {"", "end_turn"}:
            return RuntimeDecision(kind="error", error=f"模型以非预期 stop_reason 结束: {response.stop_reason}"), next_state
        return RuntimeDecision(kind="final", text=response.text), next_state

    @staticmethod
    def observe(state: RuntimeState, observations: Sequence[Observation]) -> RuntimeState:
        return RuntimeState(
            context=state.context,
            messages=state.messages + (ModelMessage("user", "动作执行结果。", observations=tuple(observations)),),
            iteration=state.iteration,
            observations=state.observations + tuple(observations),
            run_summary=state.run_summary,
            original_input_ref=state.original_input_ref,
        )

    def context_usage(
        self,
        *,
        state: RuntimeState,
        action_schemas: Sequence[Mapping[str, object]],
    ) -> ContextUsage:
        return self._token_counter.count(
            system_prompt=state.context.system_prompt,
            messages=state.messages,
            action_schemas=action_schemas,
        )

    def compact(
        self,
        *,
        state: RuntimeState,
        request: RunRequest,
        action_schemas: Sequence[Mapping[str, object]],
        target_tokens: int,
    ) -> CompactionResult:
        return self._compactor.compact(
            state=state,
            request=request,
            action_schemas=action_schemas,
            target_tokens=target_tokens,
        )
