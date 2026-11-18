"""④ 层上下文工程：只通过知识读取端口取得最小上下文。"""

from __future__ import annotations

from typing import Protocol

from harness.agents.definition import AgentDefinition
from harness.runtime.contracts import ContextSnapshot, RunRequest


class KnowledgeReader(Protocol):
    def read(self, *, agent: AgentDefinition, request: RunRequest) -> tuple[tuple[str, str], ...]: ...


class EmptyKnowledgeReader:
    def read(self, *, agent: AgentDefinition, request: RunRequest) -> tuple[tuple[str, str], ...]:
        return ()


class ContextAssembler:
    def __init__(self, reader: KnowledgeReader | None = None):
        self._reader = reader or EmptyKnowledgeReader()

    def assemble(self, *, agent: AgentDefinition, request: RunRequest) -> ContextSnapshot:
        entries = self._reader.read(agent=agent, request=request)
        knowledge = "\n\n".join(f"[{ref}]\n{text}" for ref, text in entries)
        workflow = "\n".join(
            f"- {step.id}: {step.purpose}" for step in agent.workflow_template.steps
        )
        memory_instruction = ""
        if request.memory_write_scopes:
            memory_instruction = (
                "\n记忆能力：是否提出 memory_propose 完全由你依据当前语义、Conversation 和 Agent 行为准则判断；"
                "Runtime 不按句式自动创建记忆。若你决定保存稳定事实，使用 scope=user、kind=fact、"
                "write_policy=explicit_user_memory_auto；保存偏好使用 kind=preference、"
                "write_policy=explicit_user_memory_auto。每次 memory_propose 都要自行判断并填写 sensitivity"
                "（low、normal 或 high）；该分类不替代是否保存的语义判断。更正同一事实时使用同一个 key 覆盖，"
                "不先 memory_forget；"
                "自动 user 写入的 source_ref 由 Harness 强制绑定当前回合，不要传递旧 conversation 引用；"
                "记忆动作是内部行为：除非用户明确询问记忆是否已保存，否则不要在对用户的回复中回显‘已保存/已记住/已更新记忆’等状态，"
                "只自然回应用户正在说的内容；"
                "不要把猜测写入记忆。若 action 返回失败，必须如实说明失败，不能声称已经保存。\n"
            )
        prompt = f"""{agent.identity.intro}

默认工作流：
{workflow}

{agent.identity.working_principles}

{agent.identity.concept_notes}

可用知识（按策略过滤）：
{knowledge or '(none)'}

你可以直接回答，或提出已注册 action。不要声称已执行尚未观察到结果的动作。"""
        prompt += memory_instruction
        return ContextSnapshot(system_prompt=prompt, source_refs=tuple(ref for ref, _ in entries))
