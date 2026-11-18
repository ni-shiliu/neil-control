"""① Agent 定义层 —— 注册、配置校验与渠道路由。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from harness.agents.chat import CHAT_AGENT
from harness.agents.definition import AgentDefinition
from harness.channels.request import IncomingRequest
from harness.skills.registry import SKILL_REGISTRY, SkillRegistry


class AgentRegistryError(ValueError):
    pass


class AgentRoutingError(AgentRegistryError):
    """用户请求无法安全路由到一个 Agent。"""


@dataclass(frozen=True)
class AgentRoute:
    request: IncomingRequest
    agent: AgentDefinition


class AgentRegistry:
    """唯一 Agent 来源；构造时校验产品定义，路由时校验渠道权限。"""

    def __init__(
        self,
        agents: Iterable[AgentDefinition],
        *,
        default_agents_by_channel: Mapping[str, str],
        skill_registry: SkillRegistry = SKILL_REGISTRY,
    ):
        self._agents: dict[str, AgentDefinition] = {}
        self._skill_registry = skill_registry
        for agent in agents:
            if not agent.id:
                raise AgentRegistryError("Agent id 不能为空")
            if agent.id in self._agents:
                raise AgentRegistryError(f"重复的 Agent id: {agent.id}")
            if not agent.runtime_kind.strip():
                raise AgentRegistryError(f"Agent {agent.id} 未声明 runtime_kind")
            if not agent.allowed_channels:
                raise AgentRegistryError(f"Agent {agent.id} 未声明可用渠道")
            self._skill_registry.validate_grants(agent.skill_grants)
            self._agents[agent.id] = agent
        self._default_agents_by_channel: dict[str, str] = {}
        for channel, agent_id in default_agents_by_channel.items():
            normalized_channel = channel.strip()
            if not normalized_channel:
                raise AgentRegistryError("默认 Agent 的 channel 不能为空")
            if agent_id not in self._agents:
                raise AgentRegistryError(f"渠道 {normalized_channel} 的默认 Agent 不存在: {agent_id}")
            self._default_agents_by_channel[normalized_channel] = agent_id

    def get(self, agent_id: str) -> AgentDefinition:
        try:
            return self._agents[agent_id]
        except KeyError as exc:
            raise AgentRoutingError(f"未知 Agent: @{agent_id}") from exc

    def route(self, request: IncomingRequest, *, agent_id: str | None = None) -> AgentRoute:
        """路由到受控指定 Agent，或使用渠道配置的默认 Agent。

        ``agent_id`` 仅供可信的应用/编排层传入；用户文本不参与 Agent 选择。
        """
        selected_agent_id = agent_id or self._default_agents_by_channel.get(request.channel)
        if not selected_agent_id:
            raise AgentRoutingError(f"渠道 {request.channel} 没有默认 Agent。")
        agent = self.get(selected_agent_id)
        if request.channel not in agent.allowed_channels:
            raise AgentRoutingError(f"Agent @{agent.id} 不支持 {request.channel} 渠道。")
        return AgentRoute(request=request, agent=agent)

    def list_all(self) -> list[AgentDefinition]:
        return list(self._agents.values())


REGISTRY = AgentRegistry((CHAT_AGENT,), default_agents_by_channel={"cli": CHAT_AGENT.id})


def get(agent_id: str) -> AgentDefinition:
    return REGISTRY.get(agent_id)


def list_all() -> list[AgentDefinition]:
    return REGISTRY.list_all()
