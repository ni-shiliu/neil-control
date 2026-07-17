"""chat 产品对⑥层的 Browser Adapter；具体浏览器实现保持在 tools/browser。"""

from __future__ import annotations

from typing import Callable

from harness.agents.tools.browser import actions
from harness.agents.tools.browser.diagnostics import render_browser_diagnostic, run_browser_diagnostic
from harness.capabilities import ActionManifest, CapabilityRegistry, CapabilityResult


class _BrowserHandler:
    def __init__(self, operation: Callable[[dict], object]):
        self._operation = operation

    def execute(self, request) -> CapabilityResult:
        try:
            result = self._operation(dict(request.action.proposal.input))
        except Exception as exc:
            return CapabilityResult(str(exc), success=False)
        if isinstance(result, str):
            return CapabilityResult(result)
        message = str(getattr(result, "message", result))
        state = getattr(result, "state", None)
        if state is not None:
            message = f"{message}\nurl={state.url}\ntitle={state.title}\ntext={state.text[:4000]}"
        return CapabilityResult(message, success=bool(getattr(result, "ok", True)))


def register_browser_actions(registry: CapabilityRegistry) -> None:
    """由 chat 产品/宿主显式调用；通用 Runtime 不认识这些 action。"""
    entries = (
        ("browser_open_url", "read", {"url": {"type": "string"}, "keep": {"type": "boolean"}, "wait_ms": {"type": "integer"}}, ("url",), lambda value: actions.open_url(value["url"], keep=value.get("keep", True), wait_ms=value.get("wait_ms", 2000))),
        ("browser_observe", "read", {}, (), lambda value: actions.observe_active()),
        ("browser_wait", "read", {"timeout_ms": {"type": "integer"}, "text_contains": {"type": "string"}, "url_contains": {"type": "string"}}, (), lambda value: actions.wait_for(**value)),
        ("browser_click_text", "mutation", {"text": {"type": "string"}, "exact": {"type": "boolean"}, "wait_ms": {"type": "integer"}}, ("text",), lambda value: actions.click_text(value["text"], exact=value.get("exact", False), wait_ms=value.get("wait_ms", 1000))),
        ("browser_type", "mutation", {"selector": {"type": "string"}, "text": {"type": "string"}, "wait_ms": {"type": "integer"}}, ("selector", "text"), lambda value: actions.type_text(value["selector"], value["text"], wait_ms=value.get("wait_ms", 500))),
        ("browser_diagnostic", "read", {"keep": {"type": "boolean"}}, (), lambda value: render_browser_diagnostic(run_browser_diagnostic(keep=value.get("keep", False)))),
    )
    for action_id, kind, properties, required, operation in entries:
        registry.register(
            ActionManifest(
                id=action_id,
                input_schema={"type": "object", "properties": properties, "required": list(required)},
                required_capabilities=frozenset({"browser"}), kind=kind,
                risk_level="medium" if kind == "mutation" else "low",
            ),
            _BrowserHandler(operation),
        )
