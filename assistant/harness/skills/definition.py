"""Skill 的版本化、可校验声明。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SkillManifest:
    id: str
    version: str
    action_ids: frozenset[str]
    required_capabilities: frozenset[str] = frozenset()
    risk_level: str = "low"


@dataclass(frozen=True)
class AuthorizedToolSet:
    """一次 Agent 路由解析后的唯一 action 授权事实。"""

    agent_id: str
    skill_ids: frozenset[str]
    action_ids: frozenset[str]

    @property
    def tool_names(self) -> frozenset[str]:
        """兼容旧调用方；新 Runtime 使用 action_ids。"""
        return self.action_ids
