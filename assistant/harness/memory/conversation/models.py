"""短期 Conversation 的不可变记录契约。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class ConversationRecord:
    """一条渠道回合记录；供短期上下文读取，不是长期记忆。"""

    id: str
    tenant_id: str
    user_id: str
    thread_id: str
    channel: str
    raw_text: str
    route: str
    response_text: str
    execution: dict[str, Any] = field(default_factory=dict)
    tool_calls: tuple[dict[str, Any], ...] = ()
    decision_trace: tuple[dict[str, Any], ...] = ()
    task_id: str | None = None
    run_id: str | None = None
    created_at: str = field(default_factory=now_iso)

    @property
    def source_ref(self) -> str:
        return f"conversation:{self.id}"

    def to_dict(self) -> dict[str, Any]:
        # JSONL 是人工排障时会直接打开的运行记录；时间放在每行最前面，
        # 不必横向扫描到末尾才能判断该回合发生的时间。
        return {
            "created_at": self.created_at,
            "id": self.id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "thread_id": self.thread_id,
            "channel": self.channel,
            "raw_text": self.raw_text,
            "route": self.route,
            "response_text": self.response_text,
            "execution": self.execution,
            "tool_calls": self.tool_calls,
            "decision_trace": self.decision_trace,
            "task_id": self.task_id,
            "run_id": self.run_id,
        }
