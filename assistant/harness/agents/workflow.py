"""①层 Agent 默认工作流声明。

工作流是 Agent 产品的推进骨架，而非 runtime 内的硬编码分支。当前 CLI
只执行单回合 chat workflow；后续 Task/Plan 层可将同一声明扩展为可持久化计划。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkflowStep:
    id: str
    purpose: str


@dataclass(frozen=True)
class WorkflowTemplate:
    id: str
    version: str
    summary: str
    steps: tuple[WorkflowStep, ...]

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("workflow id 不能为空")
        if not self.steps:
            raise ValueError(f"workflow {self.id} 至少需要一个步骤")
        step_ids = [step.id for step in self.steps]
        if any(not step_id for step_id in step_ids):
            raise ValueError(f"workflow {self.id} 的步骤 id 不能为空")
        if len(set(step_ids)) != len(step_ids):
            raise ValueError(f"workflow {self.id} 存在重复步骤 id")


_STEP_PATTERN = re.compile(r"^-\s+`([^`]+)`:\s*(.+)$", re.MULTILINE)


def load_workflow_markdown(path: Path) -> WorkflowTemplate:
    """从受限 Markdown 格式加载 Agent workflow 配置。"""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"无法读取 Agent workflow 配置: {path}") from exc

    if not content.startswith("---\n"):
        raise ValueError(f"Agent workflow 缺少 front matter: {path}")
    metadata_block, separator, body = content[4:].partition("\n---\n")
    if not separator:
        raise ValueError(f"Agent workflow front matter 未闭合: {path}")

    metadata: dict[str, str] = {}
    for line in metadata_block.splitlines():
        key, colon, value = line.partition(":")
        if not colon or not key.strip() or not value.strip():
            raise ValueError(f"Agent workflow front matter 格式错误: {path}")
        metadata[key.strip()] = value.strip()
    for key in ("id", "version", "summary"):
        if not metadata.get(key):
            raise ValueError(f"Agent workflow 缺少字段 {key}: {path}")

    steps = tuple(WorkflowStep(id=match.group(1).strip(), purpose=match.group(2).strip()) for match in _STEP_PATTERN.finditer(body))
    return WorkflowTemplate(
        id=metadata["id"],
        version=metadata["version"],
        summary=metadata["summary"],
        steps=steps,
    )
