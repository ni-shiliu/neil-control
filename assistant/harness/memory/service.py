"""记忆的候选、校验、晋升、归并与 TTL 服务。"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable

from harness.memory.models import CandidateMemory, MemoryRecord, MemoryScope, new_memory_id, now_iso
from harness.memory.repository import MemoryRepository


_EVIDENCE_PREFIXES = ("artifact:", "run:", "checkpoint:", "task_plan:")
_VALID_SCOPES = frozenset({"user", "project"})
_VALID_KINDS = frozenset({"preference", "fact", "decision", "summary"})
_VALID_SENSITIVITIES = frozenset({"low", "normal", "high"})


class MemoryService:
    def __init__(self, repository: MemoryRepository | None = None):
        self.repository = repository or MemoryRepository()

    def create_candidate(
        self,
        *,
        scope: str,
        tenant_id: str,
        owner_id: str,
        kind: str,
        semantic_key: str,
        content: dict,
        source_ref: str,
        source_refs: Iterable[str] = (),
        confidence: float = 1.0,
        sensitivity: str = "normal",
        ttl: str | None = None,
        write_policy: str = "evidence_required",
    ) -> CandidateMemory:
        if scope not in _VALID_SCOPES or kind not in _VALID_KINDS:
            raise ValueError("无效的记忆 scope 或 kind")
        candidate = CandidateMemory(
            id=new_memory_id("candidate"), scope=scope, tenant_id=tenant_id, owner_id=owner_id, kind=kind,
            semantic_key=semantic_key, content=dict(content), source_ref=source_ref,
            source_refs=tuple(source_refs), confidence=confidence, sensitivity=sensitivity,
            ttl=ttl, write_policy=write_policy,
        )
        return candidate

    def promote(self, candidate: CandidateMemory) -> MemoryRecord:
        self._validate_candidate(candidate)
        record = MemoryRecord(
            id=new_memory_id(),
            scope=candidate.scope, tenant_id=candidate.tenant_id, owner_id=candidate.owner_id, kind=candidate.kind,
            semantic_key=candidate.semantic_key, content=dict(candidate.content),
            source_ref=candidate.source_ref, source_refs=tuple(dict.fromkeys(candidate.source_refs)),
            confidence=candidate.confidence, sensitivity=candidate.sensitivity, ttl=candidate.ttl,
            version=1,
            write_policy=candidate.write_policy,
        )
        self.repository.save_record(record)
        return record

    def reject(self, candidate: CandidateMemory, reason: str) -> CandidateMemory:
        return replace(candidate, status="rejected", decided_at=now_iso(), rejection_reason=reason)

    def forget(self, *, scope: MemoryScope, tenant_id: str, owner_id: str, semantic_key: str) -> MemoryRecord | None:
        existing = next(
            (item for item in self.repository.list_active(scope, tenant_id, owner_id) if item.semantic_key == semantic_key),
            None,
        )
        if existing is None:
            return None
        self.repository.remove_entry(scope, tenant_id, owner_id, semantic_key)
        return replace(existing, status="tombstoned", write_policy="user_forget")

    def list_current(self, scope: MemoryScope, tenant_id: str, owner_id: str) -> list[MemoryRecord]:
        now = datetime.now(timezone.utc)
        result: list[MemoryRecord] = []
        for record in self.repository.list_active(scope, tenant_id, owner_id):
            if record.ttl and self._expired(record.ttl, now):
                self.repository.archive(record)
                continue
            result.append(record)
        return result

    def live_source_refs(self) -> set[str]:
        # Conversation 是独立短期记录，不再被长期记忆反向 pin。
        return set()

    def _validate_candidate(self, candidate: CandidateMemory) -> None:
        if candidate.sensitivity not in _VALID_SENSITIVITIES:
            raise ValueError("未知记忆敏感度")
        # sensitivity 是模型对内容的分类结果，供检索、展示和未来的治理策略
        # 使用；它不是“是否保存”的第二套硬编码决策。是否创建候选由模型提出，
        # 是否允许写入则由下方的来源、scope、写入策略和身份边界确定。
        if candidate.write_policy in {"explicit_preference_auto", "explicit_user_memory_auto"}:
            valid_kind = (
                candidate.kind == "preference"
                if candidate.write_policy == "explicit_preference_auto"
                else candidate.kind in {"preference", "fact"}
            )
            if candidate.scope != "user" or not valid_kind or not candidate.source_ref.startswith("conversation:"):
                raise ValueError("自动晋升只允许来自当前对话的明确 user preference 或稳定 fact")
            return
        if candidate.write_policy != "evidence_required":
            raise ValueError("未知的记忆写入策略")
        if not candidate.source_ref.startswith(_EVIDENCE_PREFIXES):
            raise ValueError("该记忆需要 Artifact、Run、Checkpoint 或 TaskPlan 证据")

    @staticmethod
    def _expired(ttl: str, now: datetime) -> bool:
        try:
            return datetime.fromisoformat(ttl.replace("Z", "+00:00")) <= now
        except ValueError:
            return False
