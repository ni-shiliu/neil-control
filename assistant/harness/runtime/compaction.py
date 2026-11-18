"""④ Runtime 的滚动上下文压缩；只修改当前 Run 的临时状态。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from harness.runtime.contracts import (
    ContextUsage, ModelMessage, RunRequest, RunSummary, RuntimeState,
)
from harness.runtime.model import ModelGateway
from harness.runtime.token_count import TokenCounter


_CHUNK_CHARACTERS = 100_000
_MAX_ITEM_CHARACTERS = 1_500


@dataclass(frozen=True)
class CompactionResult:
    state: RuntimeState
    before: ContextUsage
    after: ContextUsage
    mode: str
    compressed_messages: int
    input_compacted: bool = False


class RuntimeCompactor:
    """将完成的历史折叠为 RunSummary，并在必要时压缩超大当前输入。"""

    def __init__(self, *, model: ModelGateway, token_counter: TokenCounter):
        self._model = model
        self._counter = token_counter

    def compact(
        self,
        *,
        state: RuntimeState,
        request: RunRequest,
        action_schemas: Sequence[Mapping[str, Any]],
        target_tokens: int,
    ) -> CompactionResult:
        before = self._usage(state, action_schemas)
        # 初始 user message 是当前请求锚点；其余 messages 都是已经完成的
        # 回合或已观察到的 tool 结果，可以整体归并，绝不拆开事务对。
        anchor = state.messages[:1]
        history, protected_tail = self._split_unfinished_transaction(state.messages[1:])
        mode = "semantic"
        try:
            summary = self._summarize_history(state=state, request=request, history=history)
        except Exception:
            summary = self._deterministic_summary(state=state, request=request, history=history)
            mode = "deterministic"

        summary_message = ModelMessage("user", summary.render()) if summary.render() else None
        compacted_messages = anchor + ((summary_message,) if summary_message else ()) + protected_tail
        next_state = RuntimeState(
            context=state.context,
            messages=compacted_messages,
            iteration=state.iteration,
            observations=state.observations,
            run_summary=summary,
            original_input_ref=state.original_input_ref,
        )
        after = self._usage(next_state, action_schemas)

        # 如果保留的当前输入本身仍使请求过大，将其分块归并；RunRequest 保留
        # 原文，Conversation / Task 事实也不会被这里改写。
        input_compacted = False
        if after.input_tokens > target_tokens:
            try:
                input_summary = self._with_protected_context(
                    self._summarize_input(request.user_input), request,
                )
            except Exception:
                input_summary = self._with_protected_context(
                    self._deterministic_input_summary(request.user_input), request,
                )
                mode = "deterministic" if mode == "semantic" else mode
            input_message = ModelMessage(
                "user",
                "[当前用户输入已分块压缩；原文仍由当前 Run 持有]\n" + input_summary.render(),
            )
            compacted_messages = (input_message,) + ((summary_message,) if summary_message else ()) + protected_tail
            next_state = RuntimeState(
                context=state.context,
                messages=compacted_messages,
                iteration=state.iteration,
                observations=state.observations,
                run_summary=summary,
                original_input_ref=f"conversation:{request.run_id.removeprefix('request:')}",
            )
            after = self._usage(next_state, action_schemas)
            input_compacted = True

        return CompactionResult(
            state=next_state,
            before=before,
            after=after,
            mode=mode,
            compressed_messages=len(history),
            input_compacted=input_compacted,
        )

    def _usage(self, state: RuntimeState, action_schemas: Sequence[Mapping[str, Any]]) -> ContextUsage:
        return self._counter.count(
            system_prompt=state.context.system_prompt,
            messages=state.messages,
            action_schemas=action_schemas,
        )

    def _summarize_history(
        self,
        *,
        state: RuntimeState,
        request: RunRequest,
        history: Sequence[ModelMessage],
    ) -> RunSummary:
        source = "\n\n".join(filter(None, (
            state.run_summary.render() if state.run_summary else "",
            self._render_history(history),
        )))
        if not source:
            return self._deterministic_summary(state=state, request=request, history=history)
        summaries = [self._summarize_chunk(chunk) for chunk in self._chunks(source)]
        while len(summaries) > 1:
            merged = "\n\n".join(item.render() for item in summaries)
            summaries = [self._summarize_chunk(chunk) for chunk in self._chunks(merged)]
        return self._with_refs(summaries[0], state, request)

    def _summarize_input(self, user_input: str) -> RunSummary:
        summaries = [self._summarize_chunk(chunk) for chunk in self._chunks(user_input)]
        while len(summaries) > 1:
            summaries = [
                self._summarize_chunk(chunk)
                for chunk in self._chunks("\n\n".join(item.render() for item in summaries))
            ]
        return summaries[0]

    def _summarize_chunk(self, content: str) -> RunSummary:
        payload = self._model.complete_json(
            system_prompt=(
                "你是 Harness 的上下文压缩器。只输出 JSON object，字段为 "
                "objective, constraints, completed_work, decisions, observations, open_items, next_step。"
                "保留验收条件、未解决事项和 Artifact/Effect 引用；不要执行 action。"
            ),
            user_input=content,
        )
        if not isinstance(payload, dict):
            raise ValueError("压缩器未返回 JSON object")
        summary = RunSummary(
            objective=self._text(payload.get("objective")),
            constraints=self._texts(payload.get("constraints")),
            completed_work=self._texts(payload.get("completed_work")),
            decisions=self._texts(payload.get("decisions")),
            observations=self._texts(payload.get("observations")),
            open_items=self._texts(payload.get("open_items")),
            next_step=self._text(payload.get("next_step")),
        )
        if not any((summary.objective, summary.constraints, summary.completed_work, summary.decisions, summary.observations, summary.open_items, summary.next_step)):
            raise ValueError("压缩器未返回可用摘要字段")
        return summary

    @staticmethod
    def _text(value: object) -> str:
        return value.strip()[:_MAX_ITEM_CHARACTERS] if isinstance(value, str) else ""

    @classmethod
    def _texts(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return ()
        return tuple(item for raw in value if (item := cls._text(raw)))

    @staticmethod
    def _chunks(content: str) -> tuple[str, ...]:
        return tuple(content[index:index + _CHUNK_CHARACTERS] for index in range(0, len(content), _CHUNK_CHARACTERS)) or ("(empty)",)

    @staticmethod
    def _render_history(messages: Sequence[ModelMessage]) -> str:
        rendered: list[str] = []
        for message in messages:
            parts = [f"{message.role}: {message.content}"]
            parts.extend(
                f"action {action.call_id} {action.action_id}: {dict(action.input)}" for action in message.actions
            )
            parts.extend(
                f"observation {observation.call_id} {observation.action_id}: {observation.content}; "
                f"artifacts={','.join(observation.artifact_refs)}; effect={observation.effect_ref or ''}"
                for observation in message.observations
            )
            rendered.append("\n".join(parts))
        return "\n\n".join(rendered)

    @staticmethod
    def _split_unfinished_transaction(
        messages: Sequence[ModelMessage],
    ) -> tuple[tuple[ModelMessage, ...], tuple[ModelMessage, ...]]:
        """只压缩已完成的 tool-use/tool-result 对，绝不拆开未完成事务。"""
        for index, message in enumerate(messages):
            if message.role != "assistant" or not message.actions:
                continue
            expected = {action.call_id for action in message.actions}
            following = messages[index + 1] if index + 1 < len(messages) else None
            observed = (
                {item.call_id for item in following.observations}
                if following is not None and following.role == "user" else set()
            )
            if not expected <= observed:
                return tuple(messages[:index]), tuple(messages[index:])
        return tuple(messages), ()

    @classmethod
    def _deterministic_summary(
        cls,
        *,
        state: RuntimeState,
        request: RunRequest,
        history: Sequence[ModelMessage],
    ) -> RunSummary:
        observations = tuple(
            cls._text(
                f"{item.action_id}: {item.content}; artifacts={','.join(item.artifact_refs)}; effect={item.effect_ref or ''}"
            )
            for item in state.observations
        )
        return RunSummary(
            objective=cls._text(request.user_input),
            constraints=tuple(dict.fromkeys(request.protected_context)),
            completed_work=(f"已折叠 {len(history)} 条已完成 Run 消息。",),
            observations=observations,
            source_refs=cls._source_refs(state),
        )

    @classmethod
    def _deterministic_input_summary(cls, user_input: str) -> RunSummary:
        return RunSummary(
            objective=cls._text(user_input),
            completed_work=(f"原始用户输入长度为 {len(user_input)} 字符，已在当前 Run 中强制裁剪。",),
        )

    @classmethod
    def _with_refs(cls, summary: RunSummary, state: RuntimeState, request: RunRequest) -> RunSummary:
        return RunSummary(
            objective=summary.objective,
            constraints=tuple(dict.fromkeys((*request.protected_context, *summary.constraints))),
            completed_work=summary.completed_work,
            decisions=summary.decisions,
            observations=summary.observations,
            open_items=summary.open_items,
            next_step=summary.next_step,
            source_refs=tuple(dict.fromkeys((*summary.source_refs, *cls._source_refs(state)))),
        )

    @staticmethod
    def _with_protected_context(summary: RunSummary, request: RunRequest) -> RunSummary:
        return RunSummary(
            objective=summary.objective,
            constraints=tuple(dict.fromkeys((*request.protected_context, *summary.constraints))),
            completed_work=summary.completed_work,
            decisions=summary.decisions,
            observations=summary.observations,
            open_items=summary.open_items,
            next_step=summary.next_step,
            source_refs=summary.source_refs,
        )

    @staticmethod
    def _source_refs(state: RuntimeState) -> tuple[str, ...]:
        refs = list(state.context.source_refs)
        for observation in state.observations:
            refs.extend(observation.artifact_refs)
            if observation.effect_ref:
                refs.append(observation.effect_ref)
        return tuple(dict.fromkeys(refs))
