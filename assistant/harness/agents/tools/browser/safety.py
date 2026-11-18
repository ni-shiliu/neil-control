"""Safety checks for low-level browser actions."""

from __future__ import annotations

from harness.agents.tools.browser.types import BrowserAction


class BrowserSafetyError(ValueError):
    """Raised when a browser action is outside the safe primitive action set."""


_SENSITIVE_ACTION_TYPES = {
    "submit_form",
    "upload_file",
    "send_message",
    "purchase",
    "change_permission",
    "download",
    "save_password",
    "payment",
}


def validate_safe_action(action: BrowserAction) -> None:
    """Reject business-level or sensitive action types at the capability layer."""

    if action.type in _SENSITIVE_ACTION_TYPES:
        raise BrowserSafetyError(
            f"browser action requires an explicit higher-level confirmation flow: {action.type}"
        )
