"""记忆平面的运行期提案与当前 Markdown 记忆契约。"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


MemoryScope = Literal["user", "project"]
MemoryKind = Literal["preference", "fact", "decision", "summary"]
MemoryStatus = Literal["active", "superseded", "archived", "tombstoned"]
CandidateStatus = Literal["pending", "promoted", "rejected", "expired"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_memory_id(prefix: str = "memory") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    scope: MemoryScope
    tenant_id: str
    owner_id: str
    kind: MemoryKind
    semantic_key: str
    content: dict[str, Any]
    source_ref: str
    source_refs: tuple[str, ...] = ()
    confidence: float = 1.0
    sensitivity: str = "normal"
    ttl: str | None = None
    # 当前 Markdown 文档只保存一个生效值；该字段仅保留给通用上下文契约，恒为 1。
    version: int = 1
    write_policy: str = "evidence_required"
    status: MemoryStatus = "active"
    supersedes_version: int | None = None
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.id or not self.tenant_id or not self.owner_id or not self.semantic_key or not self.source_ref:
            raise ValueError("MemoryRecord 缺少 id、tenant_id、owner_id、semantic_key 或 source_ref")
        if not 0 <= self.confidence <= 1:
            raise ValueError("MemoryRecord confidence 必须介于 0 和 1")
        if self.version < 1:
            raise ValueError("MemoryRecord version 必须从 1 开始")

    @property
    def all_source_refs(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((self.source_ref, *self.source_refs)))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CandidateMemory:
    """仅在当前 Run 内存中存活的模型记忆提案，绝不落盘。"""
    id: str
    scope: MemoryScope
    tenant_id: str
    owner_id: str
    kind: MemoryKind
    semantic_key: str
    content: dict[str, Any]
    source_ref: str
    source_refs: tuple[str, ...] = ()
    confidence: float = 1.0
    sensitivity: str = "normal"
    ttl: str | None = None
    write_policy: str = "evidence_required"
    status: CandidateStatus = "pending"
    created_at: str = field(default_factory=now_iso)
    decided_at: str | None = None
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.id or not self.tenant_id or not self.owner_id or not self.semantic_key or not self.source_ref:
            raise ValueError("CandidateMemory 缺少必要字段")
        if not 0 <= self.confidence <= 1:
            raise ValueError("CandidateMemory confidence 必须介于 0 和 1")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
