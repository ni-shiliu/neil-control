"""①层知识策略：声明 Agent 可加载与可写入的知识域。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


KNOWN_KNOWLEDGE_SCOPES = frozenset({
    "shared_rules",
    "conversation",
    "memory.user",
    "memory.project",
    "personal_config",
})


@dataclass(frozen=True)
class KnowledgePolicy:
    read_scopes: frozenset[str]
    write_scopes: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        unknown = (self.read_scopes | self.write_scopes) - KNOWN_KNOWLEDGE_SCOPES
        if unknown:
            raise ValueError(f"未知知识域: {', '.join(sorted(unknown))}")
        if not self.write_scopes <= self.read_scopes:
            raise ValueError("可写知识域必须同时属于可读知识域")


def load_knowledge_markdown(path: Path) -> KnowledgePolicy:
    """从受限 Markdown front matter 加载知识读写策略。"""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"无法读取 Agent knowledge 配置: {path}") from exc

    if not content.startswith("---\n"):
        raise ValueError(f"Agent knowledge 缺少 front matter: {path}")
    metadata_block, separator, _body = content[4:].partition("\n---\n")
    if not separator:
        raise ValueError(f"Agent knowledge front matter 未闭合: {path}")

    metadata: dict[str, str] = {}
    for line in metadata_block.splitlines():
        key, colon, value = line.partition(":")
        if not colon or not key.strip():
            raise ValueError(f"Agent knowledge front matter 格式错误: {path}")
        metadata[key.strip()] = value.strip()
    if "read_scopes" not in metadata:
        raise ValueError(f"Agent knowledge 缺少字段 read_scopes: {path}")

    def scopes(key: str) -> frozenset[str]:
        return frozenset(item.strip() for item in metadata.get(key, "").split(",") if item.strip())

    return KnowledgePolicy(read_scopes=scopes("read_scopes"), write_scopes=scopes("write_scopes"))
