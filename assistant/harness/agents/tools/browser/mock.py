"""In-memory browser capability for tests and dry integration work."""

from __future__ import annotations

from dataclasses import replace

from harness.agents.tools.browser.base import BrowserCapability, BrowserSession
from harness.agents.tools.browser.safety import validate_safe_action
from harness.agents.tools.browser.types import (
    ActionResult,
    BrowserAction,
    FinalizeOptions,
    PageState,
    TabInfo,
    WaitCondition,
)


class MockBrowserSession(BrowserSession):
    def __init__(self, capability: "MockBrowserCapability", tab_id: str):
        self._capability = capability
        self._tab_id = tab_id
        self.actions: list[BrowserAction] = []
        self.finalized: FinalizeOptions | None = None

    def observe(self) -> PageState:
        return self._capability.states[self._tab_id]

    def act(self, action: BrowserAction) -> ActionResult:
        validate_safe_action(action)
        self.actions.append(action)

        state = self.observe()
        if action.type == "goto":
            if not action.url:
                return ActionResult(False, "goto action requires url", state)
            state = replace(state, url=action.url)
        elif action.type == "type":
            typed = action.text or ""
            state = replace(state, text=(state.text + typed))
        elif action.type == "wait":
            pass

        self._capability.states[self._tab_id] = state
        return ActionResult(True, f"{action.type} completed", state)

    def wait_for(self, condition: WaitCondition) -> PageState:
        state = self.observe()
        if condition.url_contains and condition.url_contains not in state.url:
            raise TimeoutError(f"url did not contain {condition.url_contains!r}")
        if condition.text_contains and condition.text_contains not in state.text:
            raise TimeoutError(f"text did not contain {condition.text_contains!r}")
        if condition.selector:
            selectors = {element.get("selector") for element in state.elements}
            if condition.selector not in selectors:
                raise TimeoutError(f"selector not found: {condition.selector}")
        return state

    def screenshot(self) -> str:
        return f"mock://screenshot/{self._tab_id}"

    def finalize(self, options: FinalizeOptions | None = None) -> None:
        self.finalized = options or FinalizeOptions()
        if not self.finalized.keep:
            self._capability.closed_tabs.append(self._tab_id)


class MockBrowserCapability(BrowserCapability):
    def __init__(self):
        self.tabs: dict[str, TabInfo] = {}
        self.states: dict[str, PageState] = {}
        self.sessions: dict[str, MockBrowserSession] = {}
        self.closed_tabs: list[str] = []
        self._next_id = 1

    def list_tabs(self) -> list[TabInfo]:
        return list(self.tabs.values())

    def open_tab(self, url: str) -> BrowserSession:
        tab_id = f"mock-tab-{self._next_id}"
        self._next_id += 1
        self.tabs[tab_id] = TabInfo(id=tab_id, title="", url=url, active=True)
        self.states[tab_id] = PageState(url=url)
        session = MockBrowserSession(self, tab_id)
        self.sessions[tab_id] = session
        return session

    def claim_tab(self, tab_id: str) -> BrowserSession:
        if tab_id not in self.tabs:
            raise KeyError(f"unknown tab id: {tab_id}")
        session = MockBrowserSession(self, tab_id)
        self.sessions[tab_id] = session
        return session
