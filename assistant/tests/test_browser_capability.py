import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engine.context import ToolRegistry
from harness.agents.tools.browser import (
    BrowserAction,
    ChromeBrowserCapability,
    FinalizeOptions,
    MockBrowserCapability,
    PageState,
    TabInfo,
    WaitCondition,
)
from harness.agents.tools.browser.diagnostics import render_browser_diagnostic, run_browser_diagnostic
from harness.agents.tools.browser.safety import BrowserSafetyError, validate_safe_action


class _FakeBridge:
    def __init__(self, responses):
        self.responses = list(responses)
        self.scripts = []

    def run_json(self, script: str):
        self.scripts.append(script)
        if not self.responses:
            raise AssertionError("no fake bridge response available")
        return self.responses.pop(0)


def test_mock_browser_open_observe_act_wait_and_finalize() -> None:
    browser = MockBrowserCapability()

    session = browser.open_tab("https://example.com")
    assert session.observe().url == "https://example.com"

    result = session.act(BrowserAction(type="goto", url="https://example.com/dashboard"))
    assert result.ok is True
    assert result.state is not None
    assert result.state.url == "https://example.com/dashboard"

    session.act(BrowserAction(type="type", text="hello"))
    state = session.wait_for(WaitCondition(url_contains="dashboard", text_contains="hello"))
    assert state.text == "hello"

    assert session.screenshot() == "mock://screenshot/mock-tab-1"
    session.finalize(FinalizeOptions(keep=False))
    assert browser.closed_tabs == ["mock-tab-1"]


def test_mock_browser_can_claim_existing_tab() -> None:
    browser = MockBrowserCapability()
    browser.tabs["existing"] = TabInfo(id="existing", title="Existing", url="https://example.com")
    browser.states["existing"] = PageState(url="https://example.com", title="Existing")

    session = browser.claim_tab("existing")

    assert session.observe().title == "Existing"


def test_browser_safety_rejects_sensitive_business_actions() -> None:
    unsafe_action = BrowserAction(type="submit_form")  # type: ignore[arg-type]

    try:
        validate_safe_action(unsafe_action)
    except BrowserSafetyError as e:
        assert "confirmation" in str(e)
    else:
        raise AssertionError("expected BrowserSafetyError")


def test_tool_registry_can_inject_browser_capability() -> None:
    tools = ToolRegistry.build(["browser"])

    assert isinstance(tools.browser, ChromeBrowserCapability)


def test_chrome_capability_lists_tabs_with_bridge() -> None:
    bridge = _FakeBridge([
        [
            {
                "id": "w1:t1",
                "title": "Example",
                "url": "https://example.com",
                "active": True,
            }
        ]
    ])
    browser = ChromeBrowserCapability(bridge=bridge)

    tabs = browser.list_tabs()

    assert tabs == [TabInfo(id="w1:t1", title="Example", url="https://example.com", active=True)]


def test_chrome_capability_opens_and_observes_tab_with_bridge() -> None:
    bridge = _FakeBridge([
        {"id": "w1:t2"},
        {
            "url": "https://example.com",
            "title": "Example",
            "text": "Hello",
            "elements": [],
            "metadata": {"tab_id": "w1:t2"},
        },
    ])
    browser = ChromeBrowserCapability(bridge=bridge)

    session = browser.open_tab("https://example.com")
    state = session.observe()

    assert state.url == "https://example.com"
    assert state.title == "Example"


def test_browser_diagnostic_reports_partial_when_javascript_is_disabled() -> None:
    bridge = _FakeBridge([
        [],
        {"id": "w1:t2"},
        {
            "url": "data:text/html,<html>",
            "title": "Chrome Capability Doctor",
            "text": "",
            "elements": [],
            "metadata": {"tab_id": "w1:t2", "javascript_enabled": False},
        },
        {
            "url": "data:text/html,<html>",
            "title": "Chrome Capability Doctor",
            "text": "",
            "elements": [],
            "metadata": {"tab_id": "w1:t2", "javascript_enabled": False},
        },
        {"ok": True},
    ])
    browser = ChromeBrowserCapability(bridge=bridge)

    result = run_browser_diagnostic(browser)
    rendered = render_browser_diagnostic(result)

    assert result.status == "partial"
    assert "javascript_from_apple_events" in rendered
    assert "Allow JavaScript from Apple Events" in rendered
    assert "type: Skipped" in rendered
