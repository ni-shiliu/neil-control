"""渠道适配层 —— IncomingRequest。

各渠道（cli / 未来 scheduler / email / telegram）把自己的输入统一转成的
瞬时对象。它是「触发器，不是事实」：不持久化、不入库、用完即弃
（对照 architecture/architecture.md §2/§3.2）。

frozen=True 在物理上表达「瞬时不可变」——与可 mutate 的持久化记录
（如 engine/records.py:RunRecord）形成对比。
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping


def _new_request_id() -> str:
    return uuid.uuid4().hex[:12]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class RequestIdentity:
    """由可信渠道适配器提供的作用域身份；Harness 不自行猜测。"""

    tenant_id: str = ""
    user_id: str = ""
    thread_id: str = ""
    project_id: str | None = None
    task_id: str | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.tenant_id and self.user_id and self.thread_id)

    def __post_init__(self) -> None:
        values = (self.tenant_id, self.user_id, self.thread_id)
        if any(values) and not all(value.strip() for value in values):
            raise ValueError("tenant_id、user_id、thread_id 必须同时提供")


@dataclass(frozen=True)
class IncomingRequest:
    channel: str                                      # "cli" | 未来 "scheduler"/"email"/...
    raw_text: str                                     # 渠道原始输入；Agent 路由不在渠道层解析
    request_id: str = field(default_factory=_new_request_id)
    created_at: str = field(default_factory=_now_iso)
    identity: RequestIdentity = field(default_factory=RequestIdentity)
    metadata: dict[str, Any] = field(default_factory=dict)  # 渠道特定附加信息（扩展位）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def create_incoming_request(
    *,
    channel: str,
    raw_text: str,
    identity: RequestIdentity | Mapping[str, Any] | None = None,
    metadata: dict[str, Any] | None = None,
) -> IncomingRequest:
    """创建跨渠道请求；调用方必须显式声明渠道标识。"""
    normalized_channel = channel.strip()
    if not normalized_channel:
        raise ValueError("channel 不能为空")
    resolved_identity = identity
    if isinstance(identity, Mapping):
        resolved_identity = RequestIdentity(
            tenant_id=str(identity.get("tenant_id", "")),
            user_id=str(identity.get("user_id", "")),
            thread_id=str(identity.get("thread_id", "")),
            project_id=str(identity["project_id"]) if identity.get("project_id") is not None else None,
            task_id=str(identity["task_id"]) if identity.get("task_id") is not None else None,
        )
    return IncomingRequest(
        channel=normalized_channel,
        raw_text=raw_text,
        identity=resolved_identity if isinstance(resolved_identity, RequestIdentity) else RequestIdentity(),
        metadata=dict(metadata or {}),
    )
