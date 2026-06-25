"""macOS Google Chrome browser capability adapter.

This adapter uses JavaScript for Automation (JXA) through ``osascript``. It is
intended as the first real transport behind the project-level browser
capability. Business loops should still depend only on BrowserCapability.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from engine.tools.browser.base import BrowserCapability, BrowserSession
from engine.tools.browser.safety import validate_safe_action
from engine.tools.browser.types import (
    ActionResult,
    BrowserAction,
    FinalizeOptions,
    PageState,
    TabInfo,
    WaitCondition,
)


class ChromeBridgeError(RuntimeError):
    """Raised when the local Chrome bridge cannot complete a command."""


class JXAChromeBridge:
    """Small subprocess wrapper around ``osascript -l JavaScript``."""

    def __init__(self, timeout_seconds: int = 15):
        self.timeout_seconds = timeout_seconds

    def run_json(self, script: str) -> Any:
        if platform.system() != "Darwin":
            raise ChromeBridgeError("Chrome JXA bridge is only supported on macOS")

        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", script],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "unknown Chrome bridge error").strip()
            raise ChromeBridgeError(message)

        output = proc.stdout.strip()
        if not output:
            return None
        try:
            return json.loads(output)
        except json.JSONDecodeError as e:
            raise ChromeBridgeError(f"Chrome bridge returned non-JSON output: {output[:200]}") from e


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def _chrome_application_js() -> str:
    app = os.getenv("CHROME_BROWSER_APP", "/Applications/Google Chrome.app")
    return f"Application({_json(app)})"


def _chrome_open_shell_command(url: str) -> str:
    bundle_id = os.getenv("CHROME_BROWSER_BUNDLE_ID", "com.google.Chrome")
    return f"/usr/bin/open -b {shlex.quote(bundle_id)} {shlex.quote(url)}"


def _split_tab_id(tab_id: str) -> tuple[int, int]:
    try:
        window_part, tab_part = tab_id.split(":")
        return int(window_part.removeprefix("w")), int(tab_part.removeprefix("t"))
    except Exception as e:
        raise ValueError(f"invalid Chrome tab id: {tab_id!r}") from e


def _tab_ref_js(tab_id: str) -> str:
    window_index, tab_index = _split_tab_id(tab_id)
    if window_index < 1 or tab_index < 1:
        raise ValueError(f"invalid Chrome tab id: {tab_id!r}")
    return f"""
function targetTab() {{
  const chrome = {_chrome_application_js()};
  if (!chrome.running()) throw new Error('Google Chrome is not running');
  const windows = chrome.windows();
  const win = windows[{window_index - 1}];
  if (!win) throw new Error('Chrome window not found: {window_index}');
  const tab = win.tabs()[{tab_index - 1}];
  if (!tab) throw new Error('Chrome tab not found: {tab_id}');
  return {{ chrome, win, tab, windowIndex: {window_index}, tabIndex: {tab_index} }};
}}
"""


def _page_elements_js() -> str:
    return """
(() => {
  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return String(value).replace(/["\\\\]/g, '\\\\$&');
  }

  function selectorFor(el) {
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    if (!tag) return '';
    if (el.id) return `${tag}#${cssEscape(el.id)}`;

    const attrs = ['name', 'placeholder', 'type', 'aria-label'];
    for (const attr of attrs) {
      const value = el.getAttribute(attr);
      if (!value) continue;
      const selector = `${tag}[${attr}="${cssEscape(value)}"]`;
      try {
        if (document.querySelectorAll(selector).length === 1) return selector;
      } catch (e) {}
    }

    const parts = [];
    let current = el;
    while (current && current.nodeType === 1 && current !== document.body) {
      let part = current.tagName.toLowerCase();
      const parent = current.parentElement;
      if (!parent) break;
      const siblings = Array.from(parent.children).filter(child => child.tagName === current.tagName);
      if (siblings.length > 1) part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
      parts.unshift(part);
      current = parent;
    }
    return parts.length ? `body > ${parts.join(' > ')}` : tag;
  }

  const nodes = Array.from(document.querySelectorAll(
    'a,button,input,textarea,select,[role="button"],[contenteditable="true"]'
  )).slice(0, 120);
  return JSON.stringify(nodes.map((el, index) => {
    const tag = el.tagName ? el.tagName.toLowerCase() : '';
    return {
      index,
      tag,
      selector: selectorFor(el),
      text: (el.innerText || el.value || '').slice(0, 160),
      aria_label: el.getAttribute('aria-label') || '',
      placeholder: el.getAttribute('placeholder') || '',
      type: el.getAttribute('type') || '',
      disabled: Boolean(el.disabled)
    };
  }));
})()
"""


class ChromeBrowserSession(BrowserSession):
    def __init__(self, bridge: JXAChromeBridge, tab_id: str):
        self._bridge = bridge
        self._tab_id = tab_id

    def observe(self) -> PageState:
        script = _tab_ref_js(self._tab_id) + f"""
const ref = targetTab();
let text = '';
let elements = [];
let javascriptEnabled = false;
try {{
  javascriptEnabled = String(ref.tab.execute({{ javascript: '1 + 1' }})) === '2';
}} catch (e) {{
  javascriptEnabled = false;
}}
try {{
  text = ref.tab.execute({{ javascript: 'document.body ? document.body.innerText : ""' }}) || '';
}} catch (e) {{
  text = '';
}}
try {{
  elements = JSON.parse(ref.tab.execute({{ javascript: {_json(_page_elements_js())} }}) || '[]');
}} catch (e) {{
  elements = [];
}}
JSON.stringify({{
  url: ref.tab.url() || '',
  title: ref.tab.title() || '',
  text,
  elements,
  metadata: {{ tab_id: {_json(self._tab_id)}, javascript_enabled: javascriptEnabled }}
}});
"""
        data = self._bridge.run_json(script)
        return PageState(
            url=data.get("url", ""),
            title=data.get("title", ""),
            text=data.get("text", ""),
            elements=data.get("elements", []),
            metadata=data.get("metadata", {}),
        )

    def act(self, action: BrowserAction) -> ActionResult:
        validate_safe_action(action)

        if action.type == "goto":
            if not action.url:
                return ActionResult(False, "goto action requires url", self.observe())
            return self._run_action_js(f"ref.tab.url = {_json(action.url)};", "goto completed")

        if action.type == "click":
            if not action.selector:
                return ActionResult(False, "click action requires selector", self.observe())
            page_js = f"""
(() => {{
  const el = document.querySelector({_json(action.selector)});
  if (!el) return 'selector not found';
  el.scrollIntoView({{ block: 'center', inline: 'center' }});
  el.click();
  return 'clicked';
}})()
"""
            return self._execute_page_js(page_js)

        if action.type == "type":
            if not action.selector:
                return ActionResult(False, "type action requires selector", self.observe())
            page_js = f"""
(() => {{
  const el = document.querySelector({_json(action.selector)});
  if (!el) return 'selector not found';
  el.focus();
  const value = {_json(action.text or '')};
  if (el.isContentEditable) {{
    el.innerText = value;
  }} else {{
    const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (setter) setter.call(el, value);
    else el.value = value;
  }}
  el.dispatchEvent(new Event('input', {{ bubbles: true }}));
  el.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return 'typed';
}})()
"""
            return self._execute_page_js(page_js)

        if action.type == "press":
            if not action.key:
                return ActionResult(False, "press action requires key", self.observe())
            page_js = f"""
(() => {{
  const key = {_json(action.key)};
  const el = document.activeElement || document.body;
  el.dispatchEvent(new KeyboardEvent('keydown', {{ key, bubbles: true }}));
  el.dispatchEvent(new KeyboardEvent('keyup', {{ key, bubbles: true }}));
  if (key === 'Enter' && el && typeof el.form?.requestSubmit === 'function') {{
    el.form.requestSubmit();
  }}
  return `pressed ${{key}}`;
}})()
"""
            return self._execute_page_js(page_js)

        if action.type == "scroll":
            direction = action.direction or "down"
            amount = action.amount if action.amount is not None else 600
            dx, dy = 0, 0
            if direction == "down":
                dy = amount
            elif direction == "up":
                dy = -amount
            elif direction == "right":
                dx = amount
            elif direction == "left":
                dx = -amount
            page_js = f"window.scrollBy({_json(dx)}, {_json(dy)}); 'scrolled';"
            return self._execute_page_js(page_js)

        if action.type == "wait":
            timeout_ms = action.timeout_ms if action.timeout_ms is not None else 1_000
            time.sleep(max(timeout_ms, 0) / 1000)
            return ActionResult(True, "wait completed", self.observe())

        return ActionResult(False, f"unsupported browser action: {action.type}", self.observe())

    def wait_for(self, condition: WaitCondition) -> PageState:
        deadline = time.monotonic() + (condition.timeout_ms / 1000)
        last_state = self.observe()
        while time.monotonic() <= deadline:
            last_state = self.observe()
            if _condition_matches(last_state, condition):
                return last_state
            time.sleep(0.25)
        raise TimeoutError("Chrome wait condition timed out")

    def screenshot(self) -> str:
        suffix = f"chrome-{self._tab_id.replace(':', '-')}.png"
        path = Path(tempfile.gettempdir()) / suffix
        proc = subprocess.run(
            ["screencapture", "-x", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise ChromeBridgeError((proc.stderr or "screencapture failed").strip())
        return str(path)

    def finalize(self, options: FinalizeOptions | None = None) -> None:
        options = options or FinalizeOptions()
        if options.keep:
            return
        script = _tab_ref_js(self._tab_id) + """
const ref = targetTab();
ref.tab.close();
JSON.stringify({ ok: true });
"""
        self._bridge.run_json(script)

    def _run_action_js(self, action_js: str, message: str) -> ActionResult:
        script = _tab_ref_js(self._tab_id) + f"""
const ref = targetTab();
{action_js}
JSON.stringify({{ ok: true }});
"""
        self._bridge.run_json(script)
        return ActionResult(True, message, self.observe())

    def _execute_page_js(self, page_js: str) -> ActionResult:
        script = _tab_ref_js(self._tab_id) + f"""
const ref = targetTab();
let message = '';
try {{
  message = String(ref.tab.execute({{ javascript: {_json(page_js)} }}) || '');
}} catch (e) {{
  JSON.stringify({{ ok: false, message: String(e) }});
}}
JSON.stringify({{ ok: Boolean(message) && message !== 'selector not found', message }});
"""
        data = self._bridge.run_json(script)
        return ActionResult(bool(data.get("ok")), data.get("message", ""), self.observe())


class ChromeBrowserCapability(BrowserCapability):
    def __init__(self, bridge: JXAChromeBridge | None = None):
        self._bridge = bridge or JXAChromeBridge()

    def list_tabs(self) -> list[TabInfo]:
        script = """
const chrome = __CHROME_APP__;
if (!chrome.running()) {
  JSON.stringify([]);
} else {
  const tabs = [];
  const windows = chrome.windows();
  for (let wi = 0; wi < windows.length; wi++) {
    const win = windows[wi];
    const activeIndex = win.activeTabIndex();
    const winTabs = win.tabs();
    for (let ti = 0; ti < winTabs.length; ti++) {
      const tab = winTabs[ti];
      tabs.push({
        id: `w${wi + 1}:t${ti + 1}`,
        title: tab.title() || '',
        url: tab.url() || '',
        active: activeIndex === ti + 1
      });
    }
  }
  JSON.stringify(tabs);
}
""".replace("__CHROME_APP__", _chrome_application_js())
        data = self._bridge.run_json(script) or []
        return [
            TabInfo(
                id=item.get("id", ""),
                title=item.get("title", ""),
                url=item.get("url", ""),
                active=bool(item.get("active", False)),
            )
            for item in data
        ]

    def open_tab(self, url: str) -> BrowserSession:
        script = """
const current = Application.currentApplication();
current.includeStandardAdditions = true;
const chrome = __CHROME_APP__;
chrome.activate();
delay(0.3);
current.doShellScript(__OPEN_COMMAND__);
delay(1.0);
const win = chrome.windows()[0];
const tabIndex = win.activeTabIndex();
JSON.stringify({ id: `w1:t${tabIndex}` });
""".replace("__CHROME_APP__", _chrome_application_js()).replace(
            "__OPEN_COMMAND__", _json(_chrome_open_shell_command(url))
        )
        data = self._bridge.run_json(script)
        return ChromeBrowserSession(self._bridge, data["id"])

    def claim_tab(self, tab_id: str) -> BrowserSession:
        _split_tab_id(tab_id)
        return ChromeBrowserSession(self._bridge, tab_id)


def _condition_matches(state: PageState, condition: WaitCondition) -> bool:
    if condition.url_contains and condition.url_contains not in state.url:
        return False
    if condition.text_contains and condition.text_contains not in state.text:
        return False
    if condition.selector:
        selectors = {element.get("selector") for element in state.elements}
        if condition.selector not in selectors:
            return False
    return True
