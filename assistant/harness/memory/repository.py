"""用户 / 项目记忆的单 Markdown 文档存储。

Conversation 是独立的按日 JSONL 证据；CandidateMemory 只存在于当前 Run，
因此这里不保存候选、版本文件或索引。每个 scope owner 只有一份可编辑文档。
"""

from __future__ import annotations

import json
import os
from hashlib import sha256
from pathlib import Path

from harness.memory.models import MemoryRecord, MemoryScope


def _segment(value: str) -> str:
    normalized = "".join(char if char.isalnum() or char in "-_." else "_" for char in value.strip())
    if not normalized or normalized in {".", ".."}:
        raise ValueError("记忆存储标识不能为空或路径非法")
    return normalized


class MemoryRepository:
    """持久化当前有效记忆，而不是其候选或历史版本。"""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or Path(__file__).with_name("memory_store")

    def document_path(self, scope: MemoryScope, tenant_id: str, owner_id: str) -> Path:
        if scope not in {"user", "project"}:
            raise ValueError(f"不支持的记忆 scope: {scope}")
        return self.base_dir / scope / _segment(tenant_id) / f"{_segment(owner_id)}.md"

    def save_record(self, record: MemoryRecord) -> None:
        """按 semantic_key 覆盖文档当前值；不保留 v1/v2 等重复文件。"""
        records = {item.semantic_key: item for item in self.list_active(record.scope, record.tenant_id, record.owner_id)}
        if record.status == "active":
            records[record.semantic_key] = record
        else:
            records.pop(record.semantic_key, None)
        self._write_document(record.scope, record.tenant_id, record.owner_id, records.values())

    def list_active(self, scope: MemoryScope, tenant_id: str, owner_id: str) -> list[MemoryRecord]:
        entries = self._read_document(scope, tenant_id, owner_id)
        path = self.document_path(scope, tenant_id, owner_id)
        records: list[MemoryRecord] = []
        for key, (kind, content) in entries.items():
            digest = sha256(f"{scope}:{tenant_id}:{owner_id}:{key}".encode()).hexdigest()[:12]
            records.append(MemoryRecord(
                id=f"memory_{digest}", scope=scope, tenant_id=tenant_id, owner_id=owner_id,
                kind=kind, semantic_key=key, content=content,
                source_ref=f"memory_document:{path}#{key}",
                write_policy="document",
            ))
        return sorted(records, key=lambda record: record.semantic_key)

    def remove_entry(self, scope: MemoryScope, tenant_id: str, owner_id: str, semantic_key: str) -> None:
        records = {
            item.semantic_key: item
            for item in self.list_active(scope, tenant_id, owner_id)
            if item.semantic_key != semantic_key
        }
        self._write_document(scope, tenant_id, owner_id, records.values())

    def archive(self, record: MemoryRecord) -> None:
        self.remove_entry(record.scope, record.tenant_id, record.owner_id, record.semantic_key)

    def _read_document(self, scope: MemoryScope, tenant_id: str, owner_id: str) -> dict[str, tuple[str, dict]]:
        try:
            lines = self.document_path(scope, tenant_id, owner_id).read_text(encoding="utf-8").splitlines()
        except OSError:
            return {}
        result: dict[str, tuple[str, dict]] = {}
        for line in lines:
            if not line.startswith("|") or line.startswith("| ---") or "key | kind | value" in line:
                continue
            parts = [part.strip().replace("\\|", "|") for part in line.strip().strip("|").split("|")]
            if len(parts) != 3 or not parts[0] or parts[1] not in {"preference", "fact", "decision", "summary"}:
                continue
            result[parts[0]] = (parts[1], self._decode_content(parts[2]))
        return result

    @staticmethod
    def _decode_content(value: str) -> dict:
        if value.startswith("{") or value.startswith("["):
            try:
                decoded = json.loads(value)
                if isinstance(decoded, dict):
                    return decoded
            except json.JSONDecodeError:
                pass
        return {"value": value}

    def _write_document(
        self, scope: MemoryScope, tenant_id: str, owner_id: str, records: object,
    ) -> None:
        title = "用户记忆" if scope == "user" else "项目记忆"
        rows = [
            f"# {title}", "",
            "可直接编辑 `value`，也可新增一行。格式：`| key | kind | value |`。",
            "支持的 kind：`preference`、`fact`、`decision`、`summary`。",
            "", "| key | kind | value |", "| --- | --- | --- |",
        ]
        for record in sorted(records, key=lambda item: item.semantic_key):
            key = record.semantic_key.replace("|", "\\|")
            value = self._encode_content(record.content).replace("|", "\\|")
            rows.append(f"| {key} | {record.kind} | {value} |")
        path = self.document_path(scope, tenant_id, owner_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text("\n".join(rows) + "\n", encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _encode_content(content: dict) -> str:
        if set(content) == {"value"} and isinstance(content["value"], str):
            return content["value"]
        return json.dumps(content, ensure_ascii=False, separators=(",", ":"))
