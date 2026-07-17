"""Conversation 写入服务。"""

from __future__ import annotations

import json

from harness.channels import IncomingRequest
from harness.interaction import Interaction
from harness.memory.conversation.models import ConversationRecord
from harness.memory.conversation.repository import ConversationRepository


class ConversationService:
    def __init__(self, repository: ConversationRepository | None = None):
        self.repository = repository or ConversationRepository()

    def record(self, *, request: IncomingRequest, interaction: Interaction, run_id: str | None = None) -> ConversationRecord | None:
        if not request.identity.is_complete:
            return None
        record = ConversationRecord(
            id=request.request_id, tenant_id=request.identity.tenant_id, user_id=request.identity.user_id,
            thread_id=request.identity.thread_id, channel=request.channel, raw_text=request.raw_text,
            route=interaction.route, response_text=interaction.text, execution=interaction.execution.to_dict(),
            tool_calls=tuple(call.to_dict() for call in interaction.tool_calls), task_id=request.identity.task_id,
            # 统一为 JSON 值形状，避免内存中的 tuple 与 JSON 读取后的 list
            # 产生不同的审计记录表示。
            decision_trace=tuple(
                json.loads(json.dumps(dict(item), ensure_ascii=False))
                for item in interaction.payload.get("decision_trace", ())
                if isinstance(item, dict)
            ),
            run_id=run_id, created_at=request.created_at,
        )
        self.repository.save(record)
        return record
