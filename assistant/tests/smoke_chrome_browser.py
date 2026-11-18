"""Manual smoke test for the real Chrome browser capability.

This script opens a temporary Chrome tab, checks basic URL/title observation,
then probes whether selector-level DOM actions are available.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from harness.agents.tools.browser import (  # noqa: E402
    BrowserAction,
    ChromeBrowserCapability,
    FinalizeOptions,
)


SMOKE_HTML = (
    "data:text/html,"
    "<html><title>Chrome Capability Smoke</title>"
    "<body>"
    "<input name=q placeholder=Query>"
    "<button id=go>Go</button>"
    "<script>"
    "document.getElementById('go').onclick=()=>document.body.append(' clicked')"
    "</script>"
    "</body></html>"
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true", help="leave the smoke tab open")
    args = parser.parse_args()

    browser = ChromeBrowserCapability()
    tabs = browser.list_tabs()
    print(f"tabs_before={len(tabs)}")

    session = browser.open_tab(SMOKE_HTML)
    session.act(BrowserAction(type="wait", timeout_ms=1_000))

    state = session.observe()
    print(f"url={state.url[:80]}")
    print(f"title={state.title}")
    print(f"javascript_enabled={state.metadata.get('javascript_enabled')}")
    print(f"elements={state.elements[:5]}")

    typed = session.act(BrowserAction(type="type", selector='input[name="q"]', text="hello"))
    clicked = session.act(BrowserAction(type="click", selector="#go"))
    final_state = session.observe()

    print(f"type_ok={typed.ok} message={typed.message!r}")
    print(f"click_ok={clicked.ok} message={clicked.message!r}")
    print(f"text={final_state.text!r}")

    session.finalize(FinalizeOptions(keep=args.keep))
    print(f"finalized keep={args.keep}")


if __name__ == "__main__":
    main()
