"""Lightweight catalogs for loop and tool capabilities."""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal


ToolSurface = Literal["loop", "chat"]


@dataclass(frozen=True)
class LoopCapability:
    name: str
    description: str
    trigger_modes: tuple[str, ...]
    required_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCapability:
    name: str
    description: str
    available_to: tuple[ToolSurface, ...]
    actions: tuple[str, ...] = ()
    risk_level: Literal["low", "medium", "high"] = "low"
    setup_hint: str = ""
    chat_tool_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapabilityCatalog:
    loops: list[LoopCapability] = field(default_factory=list)
    tools: list[ToolCapability] = field(default_factory=list)


def build_loop_capabilities(loops: dict[str, Any]) -> list[LoopCapability]:
    items: list[LoopCapability] = []
    for loop_name, loop in sorted(loops.items()):
        items.append(
            LoopCapability(
                name=loop_name,
                description=getattr(loop, "description", ""),
                trigger_modes=tuple(getattr(loop, "supported_trigger_modes", ("cron",))),
                required_tools=tuple(getattr(loop, "required_tools", ())),
            )
        )
    return items


def build_tool_capabilities() -> list[ToolCapability]:
    return [
        ToolCapability(
            name="browser",
            description="控制本机 Chrome：打开网页、读取页面状态、点击元素、输入文本、等待页面变化。",
            available_to=("loop", "chat"),
            actions=("open_url", "observe", "click_text", "type", "wait", "diagnostic"),
            risk_level="medium",
            setup_hint="首次使用需开启 Chrome -> View -> Developer -> Allow JavaScript from Apple Events。",
            chat_tool_names=(
                "browser_open_url",
                "browser_observe",
                "browser_click_text",
                "browser_type",
                "browser_wait",
                "browser_diagnostic",
            ),
        ),
        ToolCapability(
            name="claude",
            description="调用 Claude 完成生成、分析和结构化判断。",
            available_to=("loop",),
        ),
        ToolCapability(
            name="imap",
            description="读取邮箱未读邮件。",
            available_to=("loop",),
            risk_level="medium",
        ),
        ToolCapability(
            name="smtp",
            description="发送邮件或保存草稿。",
            available_to=("loop",),
            risk_level="high",
        ),
        ToolCapability(
            name="telegram",
            description="发送 Telegram 消息或文档。",
            available_to=("loop",),
            risk_level="medium",
        ),
    ]


def build_capability_catalog(loops: dict[str, Any]) -> CapabilityCatalog:
    return CapabilityCatalog(
        loops=build_loop_capabilities(loops),
        tools=build_tool_capabilities(),
    )


def render_loop_catalog(loops: list[LoopCapability]) -> str:
    if not loops:
        return "(empty)"
    lines = []
    for item in loops:
        modes = ", ".join(item.trigger_modes)
        tools = ", ".join(item.required_tools) if item.required_tools else "-"
        lines.append(f"- {item.name}: {item.description} | trigger_modes={modes} | required_tools={tools}")
    return "\n".join(lines)


def render_tool_catalog(tools: list[ToolCapability]) -> str:
    if not tools:
        return "(empty)"
    lines = []
    for item in tools:
        surfaces = ", ".join(item.available_to)
        actions = ", ".join(item.actions) if item.actions else "-"
        lines.append(
            f"- {item.name}: {item.description} | available_to={surfaces} | actions={actions} | risk={item.risk_level}"
        )
    return "\n".join(lines)
