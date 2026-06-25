"""Browser capability interfaces.

Business loops should depend on these interfaces instead of Chrome, Playwright,
or any other concrete browser-control channel.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from engine.tools.browser.types import (
    ActionResult,
    BrowserAction,
    FinalizeOptions,
    PageState,
    TabInfo,
    WaitCondition,
)


class BrowserSession(ABC):
    """A controllable browser tab/session."""

    @abstractmethod
    def observe(self) -> PageState:
        """Return the current visible/browser-readable page state."""

    @abstractmethod
    def act(self, action: BrowserAction) -> ActionResult:
        """Execute one primitive browser action."""

    @abstractmethod
    def wait_for(self, condition: WaitCondition) -> PageState:
        """Wait until the given condition is satisfied or the adapter times out."""

    @abstractmethod
    def screenshot(self) -> str:
        """Capture a screenshot and return a local path or adapter-specific handle."""

    @abstractmethod
    def finalize(self, options: FinalizeOptions | None = None) -> None:
        """Release the session, closing or keeping the tab according to options."""


class BrowserCapability(ABC):
    """Top-level browser-control capability."""

    @abstractmethod
    def list_tabs(self) -> list[TabInfo]:
        """Return tabs the adapter can see."""

    @abstractmethod
    def open_tab(self, url: str) -> BrowserSession:
        """Open a new controllable tab."""

    @abstractmethod
    def claim_tab(self, tab_id: str) -> BrowserSession:
        """Claim an existing tab by an id returned from list_tabs()."""
