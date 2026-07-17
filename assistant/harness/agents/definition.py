"""① Agent 定义层 —— 可执行的 Agent 产品声明。

本阶段只保留 CLI 已实际使用的字段。Task、知识、审批等后续能力不能用
未校验的 ``dict`` 占位混入这里，等对应运行时落地时再以明确类型加入。
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.agents.identity import IdentityProfile
from harness.agents.knowledge import KnowledgePolicy
from harness.agents.workflow import WorkflowTemplate
from harness.governance.models import DEFAULT_GOVERNANCE_PROFILE, GovernanceProfile

KNOWN_COLLABORATION_ROLES = frozenset({"planner", "coordinator", "worker", "reviewer", "operator"})

@dataclass(frozen=True)
class AgentDefinition:
    id: str
    version: str
    identity: IdentityProfile
    workflow_template: WorkflowTemplate
    knowledge_policy: KnowledgePolicy
    skill_grants: frozenset[str]
    allowed_channels: frozenset[str]
    runtime_kind: str = "chat"
    collaboration_roles: frozenset[str] = frozenset({"worker"})
    governance_profile: GovernanceProfile = DEFAULT_GOVERNANCE_PROFILE

    def __post_init__(self) -> None:
        unknown_roles = self.collaboration_roles - KNOWN_COLLABORATION_ROLES
        if unknown_roles:
            raise ValueError(f"未知协作角色: {', '.join(sorted(unknown_roles))}")
        if not self.collaboration_roles:
            raise ValueError("Agent 至少需要一个协作角色")
