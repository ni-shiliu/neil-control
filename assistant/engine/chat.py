"""
ChatHarness — CLI 聊天链路的 agentic loop 引擎。

与 LoopEngine 平行的 harness 架构：

  LoopEngine.run()                  ChatHarness.run()
  ─────────────────────────────     ────────────────────────────────
  1. 加载记忆                        1. 加载记忆 (_build_context)
  2. 构建 RunContext + tools         2. 构建 ChatContext + tool schemas
  3. _run_phases()                   3. _run_agentic_loop()
     plan→execute→verify→fix→report     while True:
                                          Claude(messages, tools)
                                          stop_reason==end_turn → break
                                          tool_use → execute → tool_result
                                          → append → continue
  4. 沉淀记忆                        4. 沉淀记忆 (_settle_memory)
  5. RunRecord                       5. conversation record (main.py)

记忆分层：
  user memory   : 用户长期偏好（仅偏好，不放记录）
  goal memory   : goal 级偏好，通过 update_goal_preferences 写入
  loop memory   : loop 级偏好，通过 update_loop_preferences 写入
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from claude_client import get_client, get_model
from engine.memory import MemoryStore
from engine.chat_tools import TOOL_SCHEMAS, execute_tool

if TYPE_CHECKING:
    from conversation_records import ConversationRecorder

log = logging.getLogger(__name__)

_ASSISTANT_DIR = Path(__file__).resolve().parent.parent
MAX_LOOP_ITERATIONS = 10  # 防止无限循环


@dataclass
class ChatContext:
    user_input: str
    goals: list[dict] = field(default_factory=list)
    loops: dict = field(default_factory=dict)
    recent_conversations: list[dict] = field(default_factory=list)
    user_memory: dict = field(default_factory=dict)
    runtime_doc: str = ""


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
        messages = [{"role": "user", "content": user_input}]

        tool_calls: list[dict] = []
        final_text = ""
        success = True

        try:
            final_text, tool_calls = self._run_agentic_loop(
                messages, system_prompt
            )
        except Exception as e:
            log.error(f"[chat] agentic loop 异常: {e}", exc_info=True)
            final_text = f"执行出错：{e}"
            success = False

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

    def _build_context(self, user_input: str, *, goals: list[dict], loops: dict) -> ChatContext:
        recent = []
        if self._conversation_recorder:
            recent = self._conversation_recorder.list_recent(limit=10)
        runtime_doc = self._read_optional_doc(_ASSISTANT_DIR / "RUNTIME.md")
        user_memory = self.memory.load_user_memory()
        return ChatContext(
            user_input=user_input,
            goals=goals,
            loops=loops,
            recent_conversations=recent,
            user_memory=user_memory,
            runtime_doc=runtime_doc,
        )

    def _build_system_prompt(self, ctx: ChatContext) -> str:
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

    # ── Agentic Loop ─────────────────────────────────────────────────────────

    def _run_agentic_loop(
        self,
        messages: list[dict],
        system_prompt: str,
    ) -> tuple[str, list[dict]]:
        """
        核心循环：Reason → tool_use → tool_result → Reason → ... → stream end_turn

        中间轮（有 tool_use）非流式，最后一轮（end_turn 纯文字）流式边收边打。
        返回 (final_text, tool_calls_log)
        """
        client = get_client()
        model = get_model()
        tool_calls_log: list[dict] = []

        for iteration in range(MAX_LOOP_ITERATIONS):
            log.debug(f"[chat] iteration={iteration}")

            # 先用非流式判断 stop_reason（tool_use 还是 end_turn）
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=system_prompt,
                tools=TOOL_SCHEMAS,
                messages=messages,
            )
            log.debug(f"[chat] stop_reason={response.stop_reason}")

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                # 纯文字回复：用流式重新请求，边收边打，给用户实时反馈
                final_text = self._stream_final_response(
                    messages=messages[:-1],  # 去掉刚才的 assistant 回复，重新 stream
                    system_prompt=system_prompt,
                )
                return final_text, tool_calls_log

            if response.stop_reason != "tool_use":
                log.warning(f"[chat] 未预期的 stop_reason: {response.stop_reason}")
                break

            # tool_use：执行所有工具，回填 tool_result，进入下一轮
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                tool_name = block.name
                tool_input = block.input if isinstance(block.input, dict) else {}

                log.info(f"[chat] tool_use name={tool_name} input={json.dumps(tool_input, ensure_ascii=False)[:200]}")
                result_text = execute_tool(tool_name, tool_input, self)
                log.info(f"[chat] tool_result name={tool_name} result={result_text[:200]}")

                tool_calls_log.append({
                    "name": tool_name,
                    "input": tool_input,
                    "result": result_text,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_text,
                })

            messages.append({"role": "user", "content": tool_results})

        log.warning(f"[chat] agentic loop 达到最大迭代次数 {MAX_LOOP_ITERATIONS}")
        return "抱歉，处理超过最大步骤数，请重新描述需求。", tool_calls_log

    def _stream_final_response(self, *, messages: list[dict], system_prompt: str) -> str:
        """最后一轮用 streaming，边收边打，返回完整文字供记录。"""
        client = get_client()
        model = get_model()
        chunks: list[str] = []

        with client.messages.stream(
            model=model,
            max_tokens=2048,
            system=system_prompt,
            tools=TOOL_SCHEMAS,
            messages=messages,
        ) as stream:
            for text in stream.text_stream:
                print(text, end="", flush=True)
                chunks.append(text)

        print()  # 换行
        return "".join(chunks)

    # ── 记忆沉淀 ─────────────────────────────────────────────────────────────

    def _settle_memory(self, tool_calls: list[dict], ctx: ChatContext) -> None:
        """
        loop 结束后沉淀记忆。
        目前只做一件事：从成功的 tool_call 里学习 goal 别名写入 user_memory。
        （偏好更新已在 tool executor 里直接写入 memory，这里不重复处理）
        """
        user_memory = self.memory.load_user_memory()
        nicknames = dict(user_memory.get("goal_nicknames", {}))
        updated = False

        for call in tool_calls:
            name = call.get("name", "")
            inp = call.get("input", {})
            result = call.get("result", "")

            # 执行成功的单目标操作，把 goal_id 和原始输入里的关键词学成别名
            if name in ("pause_goal", "resume_goal", "show_goal", "rerun_goal") and "已" in result:
                goal_id = inp.get("goal_id", "")
                # 从 user_input 里提取可能的别名（由 ctx 传入的 user_input）
                # 只在 user_input 里有明显的自然语言引用（非 goal_id 格式）时写入
                user_input = ctx.user_input
                if goal_id and not user_input.startswith("goal_") and len(user_input) <= 20:
                    if nicknames.get(user_input) != goal_id:
                        nicknames[user_input] = goal_id
                        updated = True

        if updated:
            self.memory.merge_save_user_memory({"goal_nicknames": nicknames})
            log.info(f"[chat] 学习别名更新: {nicknames}")

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

    @staticmethod
    def _read_optional_doc(path: Path) -> str:
        if not path.exists():
            return ""
        try:
            return path.read_text(encoding="utf-8").strip()
        except Exception as e:
            log.warning(f"[chat] 文档加载失败 path={path.name}: {e}")
            return ""
