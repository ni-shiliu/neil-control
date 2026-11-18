"""① Agent 定义层 —— 身份 Profile。

IdentityProfile 回答「我是谁」：角色、开场介绍、工作原则、概念说明。
只定义身份与行为准则，不含权限（权限见 AgentDefinition.skill_grants，裁决在 ⑤ 层）。

其中 intro / working_principles / concept_notes 是 system_prompt 的**静态段**，
逐字搬自原 engine/chat.py 的 _build_system_prompt；新 Harness 的动态上下文
由 Runtime 按 Agent knowledge policy 组装。
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class IdentityProfile:
    role: str                          # 角色定位一句话
    intro: str                         # system_prompt 开头段（静态）
    working_principles: str            # 「工作原则：」整段（静态，含标题）
    concept_notes: str                 # 「概念说明：」整段（静态，含标题）
    task_types: tuple[str, ...] = ()   # 面向的任务类型（占位）

    def to_dict(self) -> dict:
        return asdict(self)


_SECTION_PATTERN = re.compile(r"^##\s+([a-z_]+)\s*$", re.MULTILINE)


def load_identity_markdown(path: Path) -> IdentityProfile:
    """从受限 Markdown 格式加载 Agent 身份配置。

    不引入 YAML 解析依赖：front matter 仅支持 ``role`` 与 ``task_types``，
    正文必须包含 ``intro``、``working_principles``、``concept_notes`` 三节。
    """
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"无法读取 Agent identity 配置: {path}") from exc

    if not content.startswith("---\n"):
        raise ValueError(f"Agent identity 缺少 front matter: {path}")
    _, separator, body = content[4:].partition("\n---\n")
    if not separator:
        raise ValueError(f"Agent identity front matter 未闭合: {path}")

    metadata: dict[str, str] = {}
    for line in content[4:].split("\n---\n", 1)[0].splitlines():
        key, colon, value = line.partition(":")
        if not colon or not key.strip() or not value.strip():
            raise ValueError(f"Agent identity front matter 格式错误: {path}")
        metadata[key.strip()] = value.strip()

    sections: dict[str, str] = {}
    matches = list(_SECTION_PATTERN.finditer(body))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections[match.group(1)] = body[match.end():end].strip()

    required = ("role", "intro", "working_principles", "concept_notes")
    missing = [key for key in required if not metadata.get(key) and not sections.get(key)]
    if missing:
        raise ValueError(f"Agent identity 缺少字段 {', '.join(missing)}: {path}")

    task_types = tuple(
        item.strip() for item in metadata.get("task_types", "").split(",") if item.strip()
    )
    return IdentityProfile(
        role=metadata["role"],
        intro=sections["intro"],
        working_principles=sections["working_principles"],
        concept_notes=sections["concept_notes"],
        task_types=task_types,
    )
