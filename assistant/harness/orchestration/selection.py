"""③层的协作拓扑和目标 Agent 选择。"""

from __future__ import annotations

from harness.agents.definition import AgentDefinition
from harness.agents.registry import AgentRegistry
from harness.tasks.models import Task


class CollaborationSelectionError(ValueError):
    pass


class TopologySelector:
    """首期始终选择可执行的单 Agent 拓扑。"""

    def select(self, task: Task) -> str:
        return "single_agent"


class AgentSelector:
    def __init__(self, registry: AgentRegistry):
        self._registry = registry

    def select_worker(self, task: Task) -> AgentDefinition:
        agent = self._registry.get(task.agent_id)
        if "worker" not in agent.collaboration_roles:
            raise CollaborationSelectionError(f"Agent @{agent.id} 未获 Worker 角色")
        if agent.version != task.agent_version:
            raise CollaborationSelectionError(f"Agent @{agent.id} 版本与 Task 快照不一致")
        return agent

    def select_reviewer(self, *, producer_agent_id: str) -> AgentDefinition:
        for agent in self._registry.list_all():
            if agent.id != producer_agent_id and "reviewer" in agent.collaboration_roles:
                return agent
        raise CollaborationSelectionError("没有可用的独立 Reviewer Agent")
