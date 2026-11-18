"""
Agentic step — 通用 AI tool-use 执行步骤。

它复用 HarnessRunner，但不绑定 CLI 或具体 loop。
ChatHarness 和未来的 loop.execute()/fix() 都可以通过这里启动一次 agentic 执行。
"""

from __future__ import annotations

from typing import Any, Callable

from engine.harness import (
    HarnessHooks,
    HarnessResult,
    HarnessRunContext,
    HarnessRunner,
    HarnessState,
)


def run_agentic_step(
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    execute_tool: Callable[[str, dict[str, Any]], str],
    run_id: str = "",
    metadata: dict[str, Any] | None = None,
    direct_tools: set[str] | None = None,
    hooks: HarnessHooks | None = None,
    max_iterations: int = 10,
) -> HarnessResult:
    """运行一次 AI -> tool_use -> tool_result -> end_turn 的 agentic step。"""
    runner = HarnessRunner(
        tools=tools,
        execute_tool=execute_tool,
        is_direct_tool=lambda name: name in (direct_tools or set()),
        hooks=hooks,
        max_iterations=max_iterations,
    )
    return runner.run(
        run_ctx=HarnessRunContext(run_id=run_id, metadata=metadata or {}),
        state=HarnessState(messages=messages),
        system_prompt=system_prompt,
    )
