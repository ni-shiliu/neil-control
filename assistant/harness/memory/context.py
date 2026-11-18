"""④ 层的按 scope 记忆检索器；只返回最小上下文，不写持久化状态。"""

from __future__ import annotations

import json
from collections.abc import Iterable

from harness.agents.definition import AgentDefinition
from harness.config import PersonalConfigRepository
from harness.memory.conversation.repository import ConversationRepository
from harness.memory.models import MemoryRecord
from harness.memory.service import MemoryService
from harness.runtime.contracts import RunRequest


_BUDGETS = {
    "memory.user": 600,
    "memory.project": 1800,
    "conversation": 2400,
    "personal_config": 600,
}
_TOTAL_BUDGET = 6000


class MemoryKnowledgeReader:
    def __init__(self, *, memory: MemoryService, conversation: ConversationRepository, personal_config: PersonalConfigRepository):
        self._memory = memory
        self._conversation = conversation
        self._personal_config = personal_config

    def read(self, *, agent: AgentDefinition, request: RunRequest) -> tuple[tuple[str, str], ...]:
        identity = request.identity
        if not identity.is_complete:
            return ()
        entries: list[tuple[str, str, int]] = []
        policy = agent.knowledge_policy.read_scopes
        self._append_scope(entries, policy, "memory.user", "user", identity.tenant_id, identity.user_id)
        if identity.project_id:
            self._append_scope(entries, policy, "memory.project", "project", identity.tenant_id, identity.project_id)
        if "conversation" in policy:
            used = 0
            for record in self._conversation.list_recent(
                tenant_id=identity.tenant_id, user_id=identity.user_id, thread_id=identity.thread_id, limit=8,
            ):
                text = f"用户：{record.raw_text}\n助手：{record.response_text}"
                cost = self._estimate(text)
                if used + cost <= _BUDGETS["conversation"]:
                    entries.append((record.source_ref, text, cost))
                    used += cost
        if "personal_config" in policy:
            config = self._personal_config.load(tenant_id=identity.tenant_id, user_id=identity.user_id)
            if config:
                text = f"[personal_config]\n{json.dumps(config, ensure_ascii=False)}"
                entries.append((f"config:{identity.tenant_id}:{identity.user_id}", text, self._estimate(text)))
        selected: list[tuple[str, str]] = []
        remaining = _TOTAL_BUDGET
        for ref, text, cost in entries:
            if cost > remaining:
                continue
            selected.append((ref, text))
            remaining -= cost
        return tuple(selected)

    def _append_scope(
        self,
        entries: list[tuple[str, str, int]],
        policy: frozenset[str],
        policy_scope: str,
        memory_scope: str,
        tenant_id: str,
        owner_id: str,
    ) -> None:
        if policy_scope not in policy or not owner_id:
            return
        budget = _BUDGETS[policy_scope]
        used = 0
        records = sorted(self._memory.list_current(memory_scope, tenant_id, owner_id), key=self._priority, reverse=True)
        for record in records:
            text = self._render(record)
            cost = self._estimate(text)
            if used + cost > budget:
                continue
            entries.append((f"memory:{record.scope}:{record.id}", text, cost))
            used += cost

    @staticmethod
    def _priority(record: MemoryRecord) -> tuple[float, str]:
        return (record.confidence, record.created_at)

    @staticmethod
    def _render(record: MemoryRecord) -> str:
        return f"[{record.scope}/{record.kind}; source={record.source_ref}]\n{json.dumps(record.content, ensure_ascii=False)}"

    @staticmethod
    def _estimate(text: str) -> int:
        return max(1, (len(text) + 3) // 4)
