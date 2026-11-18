"""Harness 内核（六层架构）。

对应 architecture/architecture.md 的六层设计，逐层落地：

    harness/
      agents/         ① Agent 定义层（下一步：AgentDefinition / IdentityProfile / registry）
      tasks/          ② Task 与 Plan 层
      orchestration/  ③ 编排与协作层
      runtime/        ④ 单 Agent Runtime 层
      governance/     ⑤ 运行控制与治理层
      capabilities/   ⑥ 能力与交付层
      memory/         横切：Conversation、user / project 记忆与知识检索
      config/         横切：用户或宿主维护的个人配置

工作记忆只存在于 runtime 的 RuntimeState；conversation、memory、config
是否进入上下文由 AgentDefinition.knowledge_policy 决定。

渠道调用只需使用 ``Harness.handle(channel, raw_text, metadata=None)``；
IncomingRequest、路由和后续层都封装在内核中。
"""

__all__ = ["ExecutionState", "Harness", "Interaction", "ToolCallResult"]


def __getattr__(name: str):
    if name == "Harness":
        from harness.facade import Harness
        return Harness
    if name in {"ExecutionState", "Interaction", "ToolCallResult"}:
        from harness.interaction import ExecutionState, Interaction, ToolCallResult
        return {
            "ExecutionState": ExecutionState,
            "Interaction": Interaction,
            "ToolCallResult": ToolCallResult,
        }[name]
    raise AttributeError(name)
