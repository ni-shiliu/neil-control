"""
HarnessRunner — 通用 agentic harness 底座。

设计原则：
- RunContext 在循环外构建一次，只放稳定运行上下文
- HarnessState 在循环内推进，只放动态消息状态
- Runner 本身只负责 AI -> tool_use -> tool_result -> end_turn
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from claude_client import get_client, get_model

log = logging.getLogger(__name__)


@dataclass
class HarnessRunContext:
    """一次 run 的稳定上下文。"""

    run_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessToolCall:
    """记录工具调用，供上层沉淀记忆和写记录。"""

    name: str
    input: dict[str, Any]
    result: str


@dataclass
class HarnessState:
    """
    循环内状态。

    messages 会在每轮 tool_use 后不断增长，所以它必须属于 state，
    不能和 run 级 context 混在一起。
    """

    messages: list[dict[str, Any]]
    iteration: int = 0
    tool_calls: list[HarnessToolCall] = field(default_factory=list)
    stop_reason: str = ""
    final_text: str = ""


@dataclass
class HarnessResult:
    final_text: str
    tool_calls: list[HarnessToolCall]
    stop_reason: str
    iterations: int


@dataclass
class HarnessHooks:
    """
    Harness 生命周期 hook。

    设计成可选回调，避免把 chat / loop 的业务逻辑写死在 runner 内部。
    """

    before_model_call: Callable[[HarnessRunContext, HarnessState], None] | None = None
    after_model_response: Callable[[HarnessRunContext, HarnessState, Any], None] | None = None
    after_tool_round: Callable[[HarnessRunContext, HarnessState, list[HarnessToolCall]], None] | None = None
    on_end_turn: Callable[[HarnessRunContext, HarnessState], None] | None = None
    on_unexpected_stop: Callable[[HarnessRunContext, HarnessState], None] | None = None


class HarnessRunner:
    def __init__(
        self,
        *,
        tools: list[dict[str, Any]],
        execute_tool: Callable[[str, dict[str, Any]], str],
        is_direct_tool: Callable[[str], bool] | None = None,
        hooks: HarnessHooks | None = None,
        max_iterations: int = 10,
    ):
        self.tools = tools
        self.execute_tool = execute_tool
        self.is_direct_tool = is_direct_tool or (lambda _name: False)
        self.hooks = hooks or HarnessHooks()
        self.max_iterations = max_iterations

    def run(
        self,
        *,
        run_ctx: HarnessRunContext,
        state: HarnessState,
        system_prompt: str,
    ) -> HarnessResult:
        client = get_client()
        model = get_model()

        for iteration in range(self.max_iterations):
            state.iteration = iteration + 1
            log.debug(f"[harness] iteration={state.iteration} run={run_ctx.run_id or '-'}")
            self._call_hook(self.hooks.before_model_call, run_ctx, state)

            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=system_prompt,
                tools=self.tools,
                messages=state.messages,
            )
            state.stop_reason = getattr(response, "stop_reason", "")
            log.debug(f"[harness] stop_reason={state.stop_reason}")
            self._call_hook(self.hooks.after_model_response, run_ctx, state, response)

            state.messages.append({"role": "assistant", "content": response.content})

            if state.stop_reason == "end_turn":
                state.final_text = self._extract_text_response(response.content)
                self._call_hook(self.hooks.on_end_turn, run_ctx, state)
                return HarnessResult(
                    final_text=state.final_text,
                    tool_calls=list(state.tool_calls),
                    stop_reason=state.stop_reason,
                    iterations=state.iteration,
                )

            if state.stop_reason != "tool_use":
                log.warning(f"[harness] 未预期的 stop_reason: {state.stop_reason}")
                self._call_hook(self.hooks.on_unexpected_stop, run_ctx, state)
                break

            tool_results: list[dict[str, Any]] = []
            round_tool_calls: list[HarnessToolCall] = []
            tool_names_this_round: list[str] = []
            for block in response.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                tool_name = block.name
                tool_input = block.input if isinstance(block.input, dict) else {}
                result_text = self.execute_tool(tool_name, tool_input)

                tool_names_this_round.append(tool_name)
                tool_call = HarnessToolCall(name=tool_name, input=tool_input, result=result_text)
                state.tool_calls.append(tool_call)
                round_tool_calls.append(tool_call)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )

            state.messages.append({"role": "user", "content": tool_results})
            self._call_hook(self.hooks.after_tool_round, run_ctx, state, round_tool_calls)

            if tool_names_this_round and all(self.is_direct_tool(name) for name in tool_names_this_round):
                return HarnessResult(
                    final_text="",
                    tool_calls=list(state.tool_calls),
                    stop_reason="direct_tool",
                    iterations=state.iteration,
                )

        return HarnessResult(
            final_text="抱歉，处理超过最大步骤数，请重新描述需求。",
            tool_calls=list(state.tool_calls),
            stop_reason=state.stop_reason or "max_iterations",
            iterations=state.iteration,
        )

    @staticmethod
    def _extract_text_response(content_blocks: list[Any]) -> str:
        chunks: list[str] = []
        for block in content_blocks:
            if getattr(block, "type", "") == "text":
                text = getattr(block, "text", "")
                if text:
                    chunks.append(text)
        return "".join(chunks).strip()

    @staticmethod
    def _call_hook(hook: Callable[..., None] | None, *args: Any) -> None:
        if not hook:
            return
        hook(*args)
