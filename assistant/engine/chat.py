"""
ChatHarness — CLI 聊天链路的 agentic loop 适配器。

与 LoopEngine 的关系：

  LoopEngine.run()                  ChatHarness.run()
  ─────────────────────────────     ────────────────────────────────
  1. 加载记忆                        1. 加载记忆 (_build_context)
  2. 构建 RunContext + tools         2. 构建 ChatRuntimeContext
  3. _execute_loop_template()        3. run_agentic_step()
     plan→execute→verify→fix→report     while True:
                                          Claude(messages, tools)
                                          stop_reason==end_turn → break
                                          tool_use → execute → tool_result
                                          → append → continue
  4. 沉淀记忆                        4. 沉淀记忆 (_settle_memory)
  5. RunRecord                       5. conversation record (main.py)

Loop 和 Harness 可以组合，但边界不同：
  - LoopEngine 管目标推进和 run 生命周期
  - HarnessRunner 管 AI 多轮工具调用
  - 某个 loop 需要 agentic 能力时，可以在 execute/fix 中显式调用 HarnessRunner

记忆分层：
  user memory   : 用户长期偏好（仅偏好，不放记录）
  goal memory   : goal 级偏好，通过 update_goal_preferences 写入
  loop memory   : loop 级偏好，通过 update_loop_preferences 写入
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from engine.agentic_step import run_agentic_step
from engine.harness import HarnessHooks, HarnessRunContext, HarnessState
from engine.memory import MemoryStore
from engine.runtime_context import ChatRuntimeContext, build_chat_runtime_context
from engine.chat_tools import TOOL_SCHEMAS, execute_tool, _PRINT_DIRECT

if TYPE_CHECKING:
    from conversation_records import ConversationRecorder

log = logging.getLogger(__name__)

MAX_LOOP_ITERATIONS = 10  # 防止无限循环
_ALIAS_BLOCKLIST = (
    "暂停", "启动", "执行", "运行", "删除", "恢复", "查看", "显示",
    "列出", "有哪些", "现在", "当前", "都", "全部", "这个", "那个",
    "第", "一下", "一下子", "帮我", "给我",
)


class ChatHarness:

    def __init__(
        self,
        memory: MemoryStore | None = None,
        conversation_recorder: "ConversationRecorder | None" = None,
    ):
        self.memory = memory or MemoryStore()
        self._conversation_recorder = conversation_recorder

    # ── 主入口 ───────────────────────────────────────────────────────────────

    def run(self, user_input: str, *, goals: list[dict], loops: dict) -> dict:
        """
        agentic loop 主入口。

        返回 interaction dict 供 main.py 渲染和记录：
        { route, command, ai_result, execution }
        """
        ctx = self._build_context(user_input, goals=goals, loops=loops)
        system_prompt = self._build_system_prompt(ctx)
        final_text = ""
        success = True

        try:
            result = run_agentic_step(
                system_prompt=system_prompt,
                messages=[{"role": "user", "content": user_input}],
                tools=TOOL_SCHEMAS,
                execute_tool=lambda name, tool_input: execute_tool(name, tool_input, self),
                metadata=self._build_metadata(ctx),
                direct_tools=set(_PRINT_DIRECT),
                hooks=HarnessHooks(
                    before_model_call=self._before_model_call,
                    after_tool_round=self._after_tool_round,
                ),
                max_iterations=MAX_LOOP_ITERATIONS,
            )
            final_text = result.final_text
            tool_calls = [
                {"name": call.name, "input": call.input, "result": call.result}
                for call in result.tool_calls
            ]
        except Exception as e:
            log.error(f"[chat] agentic loop 异常: {e}", exc_info=True)
            final_text = f"执行出错：{e}"
            success = False
            tool_calls = []

        self._settle_memory(tool_calls, ctx)

        return {
            "route": "ai",
            "command": None,
            "ai_result": {"tool_calls": tool_calls, "text": final_text},
            "execution": {
                "executed": success and bool(tool_calls),
                "kind": "agentic",
                "tool_calls": [t["name"] for t in tool_calls],
            },
        }

    # ── Context 构建 ─────────────────────────────────────────────────────────

    def _build_context(self, user_input: str, *, goals: list[dict], loops: dict) -> ChatRuntimeContext:
        return build_chat_runtime_context(
            memory_store=self.memory,
            conversation_recorder=self._conversation_recorder,
            user_input=user_input,
            goals=goals,
            loops=loops,
        )

    @staticmethod
    def _build_metadata(ctx: ChatRuntimeContext) -> dict:
        return {
            "user_input": ctx.user_input,
            "goal_count": len(ctx.goals),
            "loop_count": len(ctx.loops),
        }

    @staticmethod
    def _before_model_call(run_ctx: HarnessRunContext, state: HarnessState) -> None:
        log.debug(
            "[chat] before_model_call iteration=%s goals=%s loops=%s",
            state.iteration,
            run_ctx.metadata.get("goal_count", 0),
            run_ctx.metadata.get("loop_count", 0),
        )

    @staticmethod
    def _after_tool_round(
        _run_ctx: HarnessRunContext,
        state: HarnessState,
        tool_calls,
    ) -> None:
        if tool_calls:
            log.debug(
                "[chat] after_tool_round iteration=%s tools=%s",
                state.iteration,
                [call.name for call in tool_calls],
            )

    def _build_system_prompt(self, ctx: ChatRuntimeContext) -> str:
        goals_summary = self._summarize_goals(ctx.goals)
        loops_summary = self._summarize_loops(ctx.loops)
        user_memory_summary = self._summarize_user_memory(ctx.user_memory)
        conversations_summary = self._summarize_conversations(ctx.recent_conversations)
        runtime_section = f"\n用户全局运行偏好（RUNTIME.md）：\n{ctx.runtime_doc}" if ctx.runtime_doc else ""

        return f"""你是 Neil Assistant，一个个人自动化助手的管理界面。
你通过工具（tool_use）来管理用户的自动化任务（goal）和执行计划（loop）。

当前 goals：
{goals_summary}

当前支持的 loops：
{loops_summary}

用户长期偏好（持久记忆）：
{user_memory_summary}

最近对话（短期上下文）：
{conversations_summary}
{runtime_section}

工作原则：
- 能确定用户意图时，直接调用 tool，不要反复询问确认
- 如果 goal 不明确，可以先调 list_goals 再决策
- goal_nicknames 中有别名时，优先用别名解析用户引用的目标
- 偏好类请求（"以后简报多一点 AI 新闻"）调 update_goal_preferences 或 update_loop_preferences
- 用户个人设置（"我时区是上海"）调 update_user_preferences
- 完成所有 tool 调用后，用一句话告知用户执行结果，不要冗长
- 如果确实无法处理，简短说明原因

概念说明（不要混淆）：
- loop 是已有的执行模块，不能通过对话新增；新增 loop 需要开发者写代码
- `init loop <name>` 是为已有 loop 生成或更新规则文档（.md 文件），不是创建新 loop
- goal 是基于已有 loop 创建的自动化任务，用户可以通过对话创建"""

    # ── 记忆沉淀 ─────────────────────────────────────────────────────────────

    def _settle_memory(self, tool_calls: list[dict], ctx: ChatRuntimeContext) -> None:
        """
        loop 结束后沉淀记忆。
        目前只做一件事：从成功的 tool_call 里学习 goal 别名写入 user_memory。
        （偏好更新已在 tool executor 里直接写入 memory，这里不重复处理）
        """
        user_memory = self.memory.load_user_memory()
        nicknames = self._sanitize_goal_nicknames(user_memory.get("goal_nicknames", {}))
        updated = nicknames != dict(user_memory.get("goal_nicknames", {}))

        for call in tool_calls:
            name = call.get("name", "")
            inp = call.get("input", {})
            result = call.get("result", "")

            # 执行成功的单目标操作时，只学习“稳定名字型引用”，不学习动作句。
            if name in ("pause_goal", "resume_goal", "show_goal", "rerun_goal") and "已" in result:
                goal_id = inp.get("goal_id", "")
                alias = self._extract_learnable_goal_alias(ctx.user_input)
                if goal_id and alias and nicknames.get(alias) != goal_id:
                    nicknames[alias] = goal_id
                    updated = True

        if updated:
            self.memory.merge_save_user_memory({"goal_nicknames": nicknames})
            log.info(f"[chat] 学习别名更新: {nicknames}")

    @classmethod
    def _sanitize_goal_nicknames(cls, nicknames: dict | None) -> dict[str, str]:
        if not isinstance(nicknames, dict):
            return {}
        sanitized: dict[str, str] = {}
        for alias, goal_id in nicknames.items():
            if not isinstance(alias, str) or not isinstance(goal_id, str):
                continue
            cleaned = cls._extract_learnable_goal_alias(alias)
            if cleaned:
                sanitized[cleaned] = goal_id
        return sanitized

    @classmethod
    def _extract_learnable_goal_alias(cls, user_input: str) -> str | None:
        alias = " ".join((user_input or "").split()).strip()
        if not alias:
            return None
        if alias.startswith("goal_"):
            return None
        if len(alias) < 2 or len(alias) > 12:
            return None
        lowered = alias.lower()
        if any(token in alias for token in _ALIAS_BLOCKLIST):
            return None
        if any(token in lowered for token in ("goal", "loop", "cron", "run", "rerun")):
            return None
        return alias

    # ── 辅助：context 摘要 ────────────────────────────────────────────────────

    @staticmethod
    def _summarize_goals(goals: list[dict]) -> str:
        if not goals:
            return "(empty)"
        lines = []
        for g in goals[:20]:
            lines.append(
                f"- id={g.get('id')} | status={g.get('status')} | "
                f"loop={g.get('loop')} | schedule={g.get('schedule')} | {g.get('raw')}"
            )
        return "\n".join(lines)

    @staticmethod
    def _summarize_loops(loops: dict) -> str:
        if not loops:
            return "(empty)"
        lines = []
        for loop_name, loop in sorted(loops.items()):
            trigger_modes = ", ".join(getattr(loop, "supported_trigger_modes", ("cron",)))
            lines.append(
                f"- {loop_name}: {getattr(loop, 'description', '')} | trigger_modes={trigger_modes}"
            )
        return "\n".join(lines)

    @staticmethod
    def _summarize_user_memory(user_memory: dict) -> str:
        if not user_memory:
            return "(empty)"
        lines = []
        prefs = user_memory.get("preferences")
        if prefs:
            lines.append(f"preferences: {json.dumps(prefs, ensure_ascii=False)}")
        nicknames = user_memory.get("goal_nicknames")
        if nicknames:
            nick_str = ", ".join(f'"{k}"→{v}' for k, v in nicknames.items())
            lines.append(f"goal_nicknames: {nick_str}")
        return "\n".join(lines) if lines else "(empty)"

    @staticmethod
    def _summarize_conversations(records: list[dict]) -> str:
        if not records:
            return "(empty)"
        lines = []
        for item in reversed(records[:6]):
            user_text = " ".join(str(item.get("user_input", "")).split())
            assistant_text = " ".join(str(item.get("assistant_response", "")).split())
            execution = item.get("execution") or {}
            status = "ok" if execution.get("executed") else f"failed({execution.get('reason', '')})"
            lines.append(
                f"- [{item.get('ts', '')}] status={status}\n"
                f"  user: {user_text[:120]}\n"
                f"  assistant: {assistant_text[:120]}"
            )
        return "\n".join(lines)
