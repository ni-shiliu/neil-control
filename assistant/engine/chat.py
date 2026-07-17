"""兼容模块：共享聊天运行时已迁入 ``harness.runtime``。"""

from harness.runtime.chat_runtime import ChatRuntime
from harness.interaction import Interaction


class ChatHarness(ChatRuntime):
    """兼容入口；字符串输入必须由调用方显式附带渠道。"""

    def run(
        self,
        request_or_user_input,
        *,
        channel: str | None = None,
        agent=None,
        goals: list[dict],
        loops: dict,
    ) -> Interaction:
        from harness.agents.registry import REGISTRY
        from harness.channels.request import IncomingRequest

        request = request_or_user_input
        if isinstance(request_or_user_input, str):
            if not channel:
                raise ValueError("字符串输入必须显式提供 channel")
            request = IncomingRequest(channel=channel, raw_text=request_or_user_input)
        if agent is None:
            route = REGISTRY.route(request)
            request, agent = route.request, route.agent
        return super().run(request, agent=agent, goals=goals, loops=loops)

__all__ = ["ChatHarness", "ChatRuntime"]
