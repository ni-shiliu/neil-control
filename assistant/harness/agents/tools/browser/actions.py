"""High-level browser actions shared by chat tools and future loops."""

from __future__ import annotations

from dataclasses import dataclass

from harness.agents.tools.browser.chrome_tool import ChromeBrowserCapability
from harness.agents.tools.browser.types import BrowserAction, FinalizeOptions, PageState, WaitCondition


@dataclass(frozen=True)
class BrowserActionSummary:
    ok: bool
    message: str
    state: PageState | None = None


def open_url(url: str, *, keep: bool = True, wait_ms: int = 2_000) -> BrowserActionSummary:
    if not (url.startswith("https://") or url.startswith("http://")):
        return BrowserActionSummary(False, "url must start with http:// or https://")

    browser = ChromeBrowserCapability()
    session = browser.open_tab(url)
    session.act(BrowserAction(type="wait", timeout_ms=wait_ms))
    state = session.observe()
    session.finalize(FinalizeOptions(keep=keep, status="deliverable" if keep else "omit"))
    return BrowserActionSummary(True, f"opened {state.url or url}", state)


def observe_active() -> BrowserActionSummary:
    session = ChromeBrowserCapability().claim_active_tab()
    state = session.observe()
    return BrowserActionSummary(True, "observed active tab", state)


def click_text(text: str, *, exact: bool = False, wait_ms: int = 1_000) -> BrowserActionSummary:
    session = ChromeBrowserCapability().claim_active_tab()
    state_before = session.observe()
    selector = _find_selector_by_text(state_before, text, exact=exact)
    if not selector:
        candidates = _visible_text_candidates(state_before)
        hint = f" visible text candidates: {', '.join(candidates[:20])}" if candidates else ""
        return BrowserActionSummary(False, f"text not found on active page: {text}.{hint}", state_before)

    result = session.act(BrowserAction(type="click", selector=selector))
    if wait_ms:
        session.act(BrowserAction(type="wait", timeout_ms=wait_ms))
    state = session.observe()
    return BrowserActionSummary(result.ok, result.message or f"clicked {text}", state)


def type_text(selector: str, text: str, *, wait_ms: int = 500) -> BrowserActionSummary:
    session = ChromeBrowserCapability().claim_active_tab()
    result = session.act(BrowserAction(type="type", selector=selector, text=text))
    if wait_ms:
        session.act(BrowserAction(type="wait", timeout_ms=wait_ms))
    state = session.observe()
    return BrowserActionSummary(result.ok, result.message or "typed", state)


def wait_for(
    *,
    timeout_ms: int = 2_000,
    text_contains: str | None = None,
    url_contains: str | None = None,
) -> BrowserActionSummary:
    session = ChromeBrowserCapability().claim_active_tab()
    if text_contains or url_contains:
        state = session.wait_for(
            WaitCondition(
                text_contains=text_contains,
                url_contains=url_contains,
                timeout_ms=timeout_ms,
            )
        )
        return BrowserActionSummary(True, "wait condition matched", state)

    session.act(BrowserAction(type="wait", timeout_ms=timeout_ms))
    return BrowserActionSummary(True, "wait completed", session.observe())


def _find_selector_by_text(state: PageState, text: str, *, exact: bool) -> str | None:
    target = text.strip()
    if not target:
        return None

    candidates = []
    for element in state.elements:
        element_text = str(element.get("text") or "").strip()
        if not element_text:
            continue
        matched = element_text == target if exact else target in element_text
        if matched and element.get("selector"):
            candidates.append(element)

    if not candidates:
        return None

    exact_candidates = [item for item in candidates if str(item.get("text") or "").strip() == target]
    chosen = exact_candidates[0] if exact_candidates else candidates[0]
    return str(chosen.get("selector"))


def _visible_text_candidates(state: PageState) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for element in state.elements:
        text = str(element.get("text") or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        values.append(text)
    return values
