"""Typed browser capability data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


BrowserActionType = Literal["goto", "click", "type", "press", "scroll", "wait"]


@dataclass(frozen=True)
class TabInfo:
    """A browser tab that can be opened or claimed by a capability adapter."""

    id: str
    title: str = ""
    url: str = ""
    active: bool = False


@dataclass(frozen=True)
class PageState:
    """The observable state a business loop can reason over."""

    url: str
    title: str = ""
    text: str = ""
    elements: list[dict[str, Any]] = field(default_factory=list)
    screenshot_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BrowserAction:
    """A low-level browser operation.

    This intentionally avoids business terms such as "search" or "checkout".
    Higher-level loops should translate their intent into these primitive actions.
    """

    type: BrowserActionType
    selector: str | None = None
    text: str | None = None
    url: str | None = None
    key: str | None = None
    direction: Literal["up", "down", "left", "right"] | None = None
    amount: int | float | None = None
    timeout_ms: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionResult:
    """Result returned after a browser action is attempted."""

    ok: bool
    message: str = ""
    state: PageState | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WaitCondition:
    """A condition a browser session can wait for."""

    selector: str | None = None
    url_contains: str | None = None
    text_contains: str | None = None
    timeout_ms: int = 10_000


@dataclass(frozen=True)
class FinalizeOptions:
    """How a browser session should be released."""

    keep: bool = False
    status: Literal["omit", "deliverable", "handoff"] = "omit"
