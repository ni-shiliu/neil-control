"""Browser capability diagnostics."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness.agents.tools.browser.base import BrowserCapability, BrowserSession
from harness.agents.tools.browser.chrome_tool import ChromeBrowserCapability
from harness.agents.tools.browser.types import BrowserAction, FinalizeOptions, PageState


CheckStatus = Literal["ok", "warn", "fail", "skip"]

SMOKE_HTML = (
    "data:text/html,"
    "<html><title>Chrome Capability Doctor</title>"
    "<body>"
    "<input name=q placeholder=Query>"
    "<button id=go>Go</button>"
    "<script>"
    "document.getElementById('go').onclick=()=>document.body.append(' clicked')"
    "</script>"
    "</body></html>"
)


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    status: CheckStatus
    detail: str
    fix: str = ""


@dataclass(frozen=True)
class BrowserDiagnosticResult:
    status: Literal["ok", "partial", "failed"]
    checks: list[DiagnosticCheck]


def run_browser_diagnostic(
    browser: BrowserCapability | None = None,
    *,
    keep: bool = False,
) -> BrowserDiagnosticResult:
    """Run a human-facing browser health check."""

    browser = browser or ChromeBrowserCapability()
    checks: list[DiagnosticCheck] = []
    session: BrowserSession | None = None

    chrome_app = os.getenv("CHROME_BROWSER_APP", "/Applications/Google Chrome.app")
    app_exists = Path(chrome_app).exists()
    checks.append(
        DiagnosticCheck(
            "chrome_app",
            "ok" if app_exists else "warn",
            f"Chrome app path: {chrome_app}",
            "" if app_exists else "设置 CHROME_BROWSER_APP 指向 Google Chrome.app 的绝对路径。",
        )
    )

    try:
        tabs = browser.list_tabs()
        checks.append(DiagnosticCheck("list_tabs", "ok", f"Can list tabs: {len(tabs)} tab(s) visible"))
    except Exception as e:
        checks.append(
            DiagnosticCheck(
                "list_tabs",
                "fail",
                f"Cannot list Chrome tabs: {e}",
                "确认 Chrome 已安装，并允许终端/当前应用控制 Google Chrome。",
            )
        )
        return _finalize_result(checks)

    try:
        session = browser.open_tab(SMOKE_HTML)
        checks.append(DiagnosticCheck("open_tab", "ok", "Opened diagnostic tab"))
    except Exception as e:
        checks.append(
            DiagnosticCheck(
                "open_tab",
                "fail",
                f"Cannot open diagnostic tab: {e}",
                "确认 macOS 自动化权限允许当前终端或 Codex 控制 Google Chrome。",
            )
        )
        return _finalize_result(checks)

    state: PageState | None = None
    try:
        session.act(BrowserAction(type="wait", timeout_ms=1_000))
        state = session.observe()
        title_ok = state.title == "Chrome Capability Doctor"
        checks.append(
            DiagnosticCheck(
                "observe_title",
                "ok" if title_ok else "warn",
                f"Observed title: {state.title or '(empty)'}",
            )
        )
        checks.append(
            DiagnosticCheck(
                "observe_url",
                "ok" if state.url.startswith("data:text/html") else "warn",
                f"Observed url: {state.url[:100]}",
            )
        )
    except Exception as e:
        checks.append(
            DiagnosticCheck(
                "observe",
                "fail",
                f"Cannot observe diagnostic tab: {e}",
                "确认 Chrome 标签页没有被安全弹窗、权限弹窗或浏览器策略阻挡。",
            )
        )

    javascript_enabled = bool(state and state.metadata.get("javascript_enabled"))
    checks.append(
        DiagnosticCheck(
            "javascript_from_apple_events",
            "ok" if javascript_enabled else "warn",
            f"JavaScript from Apple Events: {'enabled' if javascript_enabled else 'disabled'}",
            "" if javascript_enabled else "Chrome -> View -> Developer -> Allow JavaScript from Apple Events",
        )
    )

    if javascript_enabled and state is not None:
        checks.append(
            DiagnosticCheck(
                "dom_elements",
                "ok" if state.elements else "warn",
                f"DOM elements visible: {len(state.elements)}",
            )
        )
        _append_action_check(checks, session, BrowserAction(type="type", selector='input[name="q"]', text="hello"), "type")
        _append_action_check(checks, session, BrowserAction(type="click", selector="#go"), "click")
    else:
        checks.append(DiagnosticCheck("dom_elements", "skip", "Skipped because JavaScript from Apple Events is disabled"))
        checks.append(DiagnosticCheck("type", "skip", "Skipped because selector actions require JavaScript from Apple Events"))
        checks.append(DiagnosticCheck("click", "skip", "Skipped because selector actions require JavaScript from Apple Events"))

    if session is not None:
        try:
            session.finalize(FinalizeOptions(keep=keep))
            checks.append(DiagnosticCheck("finalize", "ok", f"Diagnostic tab finalized keep={keep}"))
        except Exception as e:
            checks.append(
                DiagnosticCheck(
                    "finalize",
                    "fail",
                    f"Cannot finalize diagnostic tab: {e}",
                    "如测试标签残留，可手动关闭 Chrome 中的 Chrome Capability Doctor 标签页。",
                )
            )

    return _finalize_result(checks)


def render_browser_diagnostic(result: BrowserDiagnosticResult) -> str:
    lines = [
        "Browser Diagnostic",
        "",
        f"Status: {result.status}",
        "",
    ]
    for check in result.checks:
        lines.append(f"[{check.status.upper():4}] {check.name}: {check.detail}")
        if check.fix:
            lines.append(f"       Fix: {check.fix}")
    return "\n".join(lines)


def _append_action_check(
    checks: list[DiagnosticCheck],
    session: BrowserSession,
    action: BrowserAction,
    name: str,
) -> None:
    try:
        result = session.act(action)
        checks.append(
            DiagnosticCheck(
                name,
                "ok" if result.ok else "warn",
                result.message or f"{name} returned no message",
            )
        )
    except Exception as e:
        checks.append(DiagnosticCheck(name, "fail", f"{name} failed: {e}"))


def _finalize_result(checks: list[DiagnosticCheck]) -> BrowserDiagnosticResult:
    statuses = {check.status for check in checks}
    if "fail" in statuses:
        overall = "failed"
    elif statuses & {"warn", "skip"}:
        overall = "partial"
    else:
        overall = "ok"
    return BrowserDiagnosticResult(overall, checks)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run browser capability diagnostics")
    parser.add_argument("--keep", action="store_true", help="leave the diagnostic tab open")
    args = parser.parse_args()
    print(render_browser_diagnostic(run_browser_diagnostic(keep=args.keep)))


if __name__ == "__main__":
    main()
