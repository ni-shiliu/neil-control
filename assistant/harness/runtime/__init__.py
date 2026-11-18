"""④ 层：可被多个聊天型 Agent 复用的运行时。"""

__all__ = [
    "AgentRuntime", "ChatInputs", "ChatInputsProvider", "ChatRuntime", "TaskIntake",
    "ActionProposal", "AnthropicModelGateway", "ContextAssembler", "ContextSnapshot", "ContextUsage",
    "ControlledRun", "EmptyKnowledgeReader", "KnowledgeReader", "ModelGateway",
    "ModelMessage", "ModelResponse", "Observation", "RunRequest", "RunScope",
    "Runtime", "RuntimeCompactor", "RuntimeDecision", "RuntimeOutcome", "RuntimeState", "RunSummary",
    "GatewayTokenCounter", "TokenCounter",
]


def __getattr__(name: str):
    # Task 数据层和其测试不应因聊天模型依赖尚未安装而无法导入。
    if name == "ChatRuntime":
        from harness.runtime.chat_runtime import ChatRuntime
        return ChatRuntime
    if name in {"AgentRuntime", "ChatInputs", "ChatInputsProvider"}:
        from harness.runtime.agent_runtime import AgentRuntime, ChatInputs, ChatInputsProvider
        return {
            "AgentRuntime": AgentRuntime,
            "ChatInputs": ChatInputs,
            "ChatInputsProvider": ChatInputsProvider,
        }[name]
    if name == "TaskIntake":
        from harness.runtime.task_intake import TaskIntake
        return TaskIntake
    if name in {
        "ActionProposal", "ContextSnapshot", "ContextUsage", "ModelMessage", "ModelResponse", "Observation",
        "RunRequest", "RunScope", "RunSummary", "RuntimeDecision", "RuntimeOutcome", "RuntimeState",
    }:
        from harness.runtime import contracts
        return getattr(contracts, name)
    if name in {"ContextAssembler", "EmptyKnowledgeReader", "KnowledgeReader"}:
        from harness.runtime import context
        return getattr(context, name)
    if name in {"AnthropicModelGateway", "ModelGateway"}:
        from harness.runtime import model
        return getattr(model, name)
    if name == "Runtime":
        from harness.runtime.runtime import Runtime
        return Runtime
    if name == "RuntimeCompactor":
        from harness.runtime.compaction import RuntimeCompactor
        return RuntimeCompactor
    if name in {"GatewayTokenCounter", "TokenCounter"}:
        from harness.runtime import token_count
        return getattr(token_count, name)
    if name == "ControlledRun":
        from harness.runtime.runner import ControlledRun
        return ControlledRun
    raise AttributeError(name)
