"""
邮件 Loop。

plan   → 从 IMAP 拉取未读邮件，历史 pattern 从记忆读取
execute → 对每封邮件：白名单/should_reply → Maker-Checker → 产出 effect 意图
verify  → 检查是否有处理失败的邮件
fix    → 对失败邮件兜底生成草稿 effect
report  → 格式化摘要字符串 + Telegram 通知

Loop Engineering 扩展：
  - is_goal_met()    收件箱零未读时达成目标
  - next_trigger()   未达成时 30 分钟后重试
  - extract_memory() 沉淀跳过的发件人 pattern，下次直接用
"""

import logging
import os
from datetime import timedelta
from typing import TYPE_CHECKING

from engine.agentic_step import run_agentic_step
from loops.base import BaseLoop

if TYPE_CHECKING:
    from engine.context import RunContext

log = logging.getLogger(__name__)

BODY_LIMIT_FOR_CLAUDE = 2000
AUTO_SEND_CONFIDENCE = int(os.environ.get("EMAIL_AUTO_SEND_CONFIDENCE", 75))

# 静态白名单（兜底；记忆中的动态 pattern 优先）
AUTO_DRAFT_SENDERS = {
    "noreply@github.com",
    "@notice.aliyun.com",
    "@alibabacloud.com",
    "@amazonaws.com",
    "@notifications.google.com",
    "@apple.com",
    "@microsoft.com",
    "@tencent.com",
    "@cloud.tencent.com",
    "@feishu.cn",
    "@larksuite.com",
    "@dingtalk.com",
    "@mailer.substack.com",
    "@email.gitlab.com",
    "@jira.atlassian.com",
    "@bitbucket.org",
}


class EmailLoop(BaseLoop):

    name = "email_loop"
    description = "处理/回复邮件：拉取 163 邮箱未读邮件，Claude 分析生成回复，自动发送或存草稿"
    required_tools = ["imap", "smtp", "claude", "telegram"]
    supported_trigger_modes = ("cron", "goal", "event")
    use_loop_doc = True

    def __init__(self, agent_mode: str = "semi_auto", max_emails: int = 10,
                 auto_draft_senders: set[str] | None = None):
        self.agent_mode = agent_mode
        self.max_emails = max_emails
        self._static_whitelist = auto_draft_senders or AUTO_DRAFT_SENDERS

    # ── Loop Engineering 钩子 ────────────────────────────

    def is_goal_met(self, result: dict, memory: dict) -> bool:
        return result.get("unread_count", 0) == 0

    def next_trigger(self, result: dict) -> timedelta | None:
        if result.get("unread_count", 0) > 0:
            return timedelta(minutes=30)
        return None  # 收件箱清空，等 IMAP IDLE 事件触发

    @staticmethod
    def _normalize_sender(sender: str) -> str:
        return sender.lower().strip()

    @staticmethod
    def _is_own_sender(sender: str) -> bool:
        email_user = os.environ.get("EMAIL_USER", "")
        return bool(email_user) and EmailLoop._normalize_sender(sender) == email_user.lower().strip()

    def extract_memory(self, result: dict, old_memory: dict) -> dict:
        """loop 级记忆只保留跨 goal 的轻量聚合统计。"""
        totals = dict(old_memory.get("totals", {}))
        totals["runs"] = int(totals.get("runs", 0)) + 1
        totals["sent"] = int(totals.get("sent", 0)) + len(result.get("sent", []))
        totals["drafted"] = int(totals.get("drafted", 0)) + len(result.get("drafted", []))
        totals["skipped"] = int(totals.get("skipped", 0)) + len(result.get("skipped", []))
        totals["failed"] = int(totals.get("failed", 0)) + len(result.get("failed", []))
        return {
            **old_memory,
            "totals": totals,
        }

    def extract_goal_memory(self, result: dict, old_memory: dict) -> dict:
        """goal 级记忆沉淀该目标专属的跳过规则和近期处理状态。"""
        patterns = [
            p for p in old_memory.get("skip_patterns", [])
            if not self._is_own_sender(p.get("sender", ""))
        ]
        seen = {self._normalize_sender(p["sender"]) for p in patterns if p.get("sender")}
        for s in result.get("skipped", []):
            sender = s.get("sender", "")
            normalized = self._normalize_sender(sender)
            if not sender or self._is_own_sender(sender) or normalized in seen:
                continue
            patterns.append({"sender": sender, "reason": s.get("reason", "")})
            seen.add(normalized)
        recent_activity = list(old_memory.get("recent_activity", []))
        last_counts = {
            "unread_count": result.get("unread_count", 0),
            "sent_count": len(result.get("sent", [])),
            "drafted_count": len(result.get("drafted", [])),
            "skipped_count": len(result.get("skipped", [])),
            "failed_count": len(result.get("failed", [])),
        }
        recent_activity.append(last_counts)
        recent_subjects = {
            "sent": [item.get("subject", "") for item in result.get("sent", [])[:5] if item.get("subject")],
            "drafted": [item.get("subject", "") for item in result.get("drafted", [])[:5] if item.get("subject")],
            "skipped": [item.get("subject", "") for item in result.get("skipped", [])[:5] if item.get("subject")],
            "failed": [item.get("subject", "") for item in result.get("failed", [])[:5] if item.get("subject")],
        }
        last_summary = (
            f"sent={last_counts['sent_count']} "
            f"drafted={last_counts['drafted_count']} "
            f"skipped={last_counts['skipped_count']} "
            f"failed={last_counts['failed_count']} "
            f"unread={last_counts['unread_count']}"
        )
        return {
            **old_memory,
            "skip_patterns": patterns[-50:],
            "unread_count": result.get("unread_count", 0),
            "last_counts": last_counts,
            "last_summary": last_summary,
            "last_subjects": recent_subjects,
            "recent_activity": recent_activity[-5:],
        }

    # ── 白名单：静态 + 记忆动态 pattern ─────────────────

    def _should_auto_skip(self, sender: str, memory: dict) -> str | None:
        """检查发件人是否应跳过。命中返回原因，否则 None。"""
        s = self._normalize_sender(sender)

        # 1. 静态白名单
        for pattern in self._static_whitelist:
            p = pattern.lower().strip()
            if p.startswith("@"):
                if s.endswith(p) and s.count("@") == 1:
                    return f"发件人域名 {p} 在白名单中"
            elif s == p:
                return f"发件人 {p} 在白名单中"

        # 2. 记忆中的动态 pattern
        for pat in memory.get("skip_patterns", []):
            if pat.get("sender", "").lower() == s:
                return f"历史记录：{pat.get('reason', '曾被跳过')}"

        return None

    @staticmethod
    def _merge_preferences(base: dict, overrides: dict) -> dict:
        merged = dict(base)
        for key, value in overrides.items():
            if isinstance(merged.get(key), dict) and isinstance(value, dict):
                merged[key] = EmailLoop._merge_preferences(merged[key], value)
            else:
                merged[key] = value
        return merged

    def _resolved_preferences(self, ctx: "RunContext | None" = None) -> dict:
        if not ctx:
            return {}
        loop_preferences = ctx.memory.get("preferences", {})
        goal_preferences = ctx.goal_memory.get("preferences", {})
        if not isinstance(loop_preferences, dict):
            loop_preferences = {}
        if not isinstance(goal_preferences, dict):
            goal_preferences = {}
        return self._merge_preferences(loop_preferences, goal_preferences)

    @staticmethod
    def _effect_key(uid: str, action: str) -> str:
        return f"email:{uid}:{action}"

    # ── Claude 调用（使用注入的 ClaudeTool）──────────────

    def _call_claude(self, prompt: str, ctx: "RunContext | None" = None) -> dict:
        if ctx and ctx.tools.claude:
            return ctx.tools.claude.complete_json(prompt)
        # 降级：直接调（兼容旧测试）
        import json
        from claude_client import get_client, get_model
        msg = get_client().messages.create(
            model=get_model(), max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            parts = raw.split("```")
            raw = parts[1].lstrip("json").strip() if len(parts) > 1 else raw
        result = json.loads(raw)
        return self._coerce_bools(result)

    @staticmethod
    def _coerce_bools(d: dict) -> dict:
        for k, v in list(d.items()):
            if isinstance(v, str) and v.lower() in ("true", "false"):
                d[k] = v.lower() == "true"
        return d

    def _should_reply(self, sender: str, subject: str, body: str,
                      ctx: "RunContext | None" = None) -> dict:
        return self._call_claude(f"""你是邮件分类助手，判断一封邮件是否需要人工回复。

发件人：{sender}
主题：{subject}
正文：{body[:BODY_LIMIT_FOR_CLAUDE]}

以下情况【不需要回复】：
- 系统自动通知（账单、到期提醒、安全告警、服务变更通知）
- 营销/订阅/Newsletter（周刊、产品更新、推广邮件）
- 退信通知（Delivery Status Notification、mailer-daemon）
- 无需确认的单向通知（激活成功、支付成功、发货通知）
- 发件人是 noreply/no-reply/do-not-reply 开头

以下情况【需要回复】：
- 真人发来的问询、请求、邀请
- 需要确认的事项（会议、合作、审批）
- 客诉或需要跟进的工单

输出严格 JSON：
{{"need_reply":true或false,"reason":"一句话说明原因","summary":"一句话概括邮件内容（不超过30字）"}}""", ctx)

    def _generate_reply(self, uid: str, sender: str, subject: str, body: str,
                        ctx: "RunContext | None" = None) -> dict:
        if self.agent_mode == "full_auto":
            return self._full_auto_reply(sender, subject, body, ctx)
        return self._semi_auto_reply(sender, subject, body, ctx)

    def _semi_auto_reply(self, sender: str, subject: str, body: str,
                         ctx: "RunContext | None" = None) -> dict:
        return self._call_claude(f"""你是专业邮件助手，帮用户回复工作邮件。

发件人：{sender}
主题：{subject}
正文：{body[:BODY_LIMIT_FOR_CLAUDE]}

风险判断（high/low）——只看回复本身的风险，不看邮件内容：
- low（可自动回复）：
  · 会议/活动邀请的出席确认
  · 日常问候、简单致谢
  · 收件确认、已阅知悉类回复
  · 约定好的信息同步（进度播报、状态更新）
- high（需人工确认）：
  · 涉及金额、合同、法律条款
  · 做出具体承诺或答应对方某件事
  · 投诉、纠纷、敏感话题
  · 你不确定对方真实意图

输出严格 JSON：
{{"risk_level":"low或high","risk_reason":"high时说明，low时为空","confidence":0到100,"reply":"回复正文不超过200字，段落之间用\\n\\n分隔"}}""", ctx)

    def _full_auto_reply(self, sender: str, subject: str, body: str,
                         ctx: "RunContext | None" = None) -> dict:
        result = self._call_claude(f"""你是专业邮件助手，拥有完全处理权限。

发件人：{sender}
主题：{subject}
正文：{body[:BODY_LIMIT_FOR_CLAUDE]}

action 选项：
- reply_now：可直接安全回复
- save_draft：需人工确认
- escalate：超出自动处理范围

输出严格 JSON：
{{"action":"reply_now或save_draft或escalate","reason":"原因","confidence":0到100,"reply":"回复正文，段落之间用\\n\\n分隔"}}""", ctx)
        action = result.get("action", "save_draft")
        return {
            "risk_level": "low" if action == "reply_now" else "high",
            "risk_reason": result.get("reason", ""),
            "confidence": result.get("confidence", 0),
            "reply": result.get("reply", ""),
        }

    def _verify_reply(self, sender: str, subject: str, body: str, reply: str,
                      ctx: "RunContext | None" = None) -> dict:
        return self._call_claude(f"""你是邮件回复质量审查员。

原始邮件主题：{subject}
原始正文：{body[:BODY_LIMIT_FOR_CLAUDE]}
待审查回复：{reply}

检查：1)是否切题 2)语气是否得体 3)是否有不当承诺 4)语言是否流畅

输出严格 JSON：
{{"pass":true或false,"issues":"不通过时说明，通过时为空"}}""", ctx)

    # ── BaseLoop 五个抽象方法 ─────────────────────────────

    def plan(self, goal: dict, ctx: "RunContext | None" = None) -> dict:
        memory = ctx.goal_memory if ctx else {}
        if ctx and ctx.tools.imap:
            emails = ctx.tools.imap.fetch_unseen(limit=self.max_emails)
        else:
            # 降级：直接用 IMAPTool（兼容旧测试）
            from harness.agents.tools.imap_tool import IMAPTool
            emails = IMAPTool().fetch_unseen(limit=self.max_emails)
        return {"emails": emails, "memory": memory}

    def execute(self, context: dict, ctx: "RunContext | None" = None) -> dict:
        sent, drafted, skipped, failed = [], [], [], []
        memory = context.get("memory", {})

        effects = ctx.effects if ctx else None

        for em in context["emails"]:
            uid, sender, subject, body = em["uid"], em["sender"], em["subject"], em["body"]
            try:
                # 白名单 + 历史 pattern 跳过
                skip_reason = self._should_auto_skip(sender, memory)
                if skip_reason:
                    log.info(f"跳过: {sender} | {skip_reason}")
                    success_item = {
                        "uid": uid,
                        "subject": subject,
                        "sender": sender,
                        "reason": skip_reason,
                    }
                    failure_item = {
                        "uid": uid,
                        "subject": subject,
                        "sender": sender,
                        "reason": skip_reason,
                    }
                    if effects is not None:
                        effects.add(
                            "mark_read",
                            {"uid": uid},
                            {
                                "success_bucket": "skipped",
                                "success_item": success_item,
                                "failure_item": failure_item,
                            },
                            idempotency_key=self._effect_key(uid, "skip_mark_read"),
                        )
                    else:
                        skipped.append(success_item)
                    continue

                self._process_email_with_harness(
                    email=em,
                    ctx=ctx,
                    sent=sent,
                    drafted=drafted,
                    skipped=skipped,
                )
            except Exception as e:
                log.error(f"处理邮件失败 uid={uid}: {e}")
                failed.append({"uid": uid, "subject": em.get("subject", ""),
                               "sender": em.get("sender", ""), "error": str(e)})

        return {"sent": sent, "drafted": drafted, "skipped": skipped, "failed": failed,
                "unread_count": 0}

    def _process_email_with_harness(
        self,
        *,
        email: dict,
        ctx: "RunContext | None",
        sent: list[dict],
        drafted: list[dict],
        skipped: list[dict],
    ) -> None:
        uid, sender, subject, body = email["uid"], email["sender"], email["subject"], email["body"]
        tools = self._email_tool_schemas(ctx)

        result = run_agentic_step(
            system_prompt=self._build_email_agent_prompt(ctx),
            messages=[{
                "role": "user",
                "content": self._build_email_agent_message(sender, subject, body),
            }],
            tools=tools,
            execute_tool=lambda name, tool_input: self._execute_email_agent_tool(
                name,
                tool_input,
                email=email,
                ctx=ctx,
                sent=sent,
                drafted=drafted,
                skipped=skipped,
            ),
            run_id=getattr(ctx, "run_id", "") if ctx else "",
            metadata={
                "loop": self.name,
                "uid": uid,
                "sender": sender,
                "subject": subject,
            },
            direct_tools={tool["name"] for tool in tools},
            max_iterations=4,
        )
        if not result.tool_calls:
            raise RuntimeError("邮件 agent 未选择任何处理动作")

    def _build_email_agent_prompt(self, ctx: "RunContext | None") -> str:
        preferences = self._resolved_preferences(ctx)
        behavior_preferences = preferences.get("behavior", {})
        draft_first = bool(behavior_preferences.get("draft_first", False))
        auto_send = os.environ.get("EMAIL_AUTO_SEND", "false").lower() == "true"
        if draft_first:
            auto_send = False

        policy = [
            "你是邮件处理 agent。你必须且只能调用一个工具完成当前邮件处理。",
            "可选动作：无需回复则调用 skip_email；需要人工确认则调用 save_draft；只有明确安全时才调用 send_reply。",
            "系统通知、营销订阅、账单提醒、退信、noreply 类邮件通常 skip_email。",
            "真人问询、会议邀请、合作请求、审批事项通常需要回复。",
            "回复必须简洁、礼貌、具体，不超过 200 字。",
            "不要做金额、合同、法律或敏感承诺；这类邮件必须 save_draft。",
            f"自动发送开关：{'开启' if auto_send else '关闭'}。",
            f"自动发送置信度阈值：{AUTO_SEND_CONFIDENCE}。",
        ]
        if not auto_send:
            policy.append("自动发送关闭时，即使你生成了回复，也必须调用 save_draft，不能调用 send_reply。")
        if draft_first:
            policy.append("用户偏好要求优先存草稿，必须调用 save_draft，不能调用 send_reply。")
        if ctx and ctx.loop_doc:
            policy.append(f"\nLoop 规则文档：\n{ctx.loop_doc}")
        return "\n".join(policy)

    @staticmethod
    def _build_email_agent_message(sender: str, subject: str, body: str) -> str:
        return (
            f"发件人：{sender}\n"
            f"主题：{subject}\n"
            f"正文：{body[:BODY_LIMIT_FOR_CLAUDE]}"
        )

    def _email_tool_schemas(self, ctx: "RunContext | None" = None) -> list[dict]:
        preferences = self._resolved_preferences(ctx)
        behavior_preferences = preferences.get("behavior", {})
        if not isinstance(behavior_preferences, dict):
            behavior_preferences = {}
        draft_first = bool(behavior_preferences.get("draft_first", False))
        auto_send = os.environ.get("EMAIL_AUTO_SEND", "false").lower() == "true" and not draft_first

        tools = [
            {
                "name": "skip_email",
                "description": "标记当前邮件为已读并跳过，不生成回复。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reason": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["reason"],
                },
            },
            {
                "name": "save_draft",
                "description": "保存回复草稿并标记当前邮件已读，适合需要人工确认或自动发送关闭的邮件。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reply": {"type": "string"},
                        "reason": {"type": "string"},
                        "confidence": {"type": "integer"},
                    },
                    "required": ["reply", "reason"],
                },
            },
        ]
        if auto_send:
            tools.append({
                "name": "send_reply",
                "description": "直接发送回复并标记当前邮件已读。只用于低风险且置信度足够高的邮件。",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "reply": {"type": "string"},
                        "reason": {"type": "string"},
                        "confidence": {"type": "integer"},
                    },
                    "required": ["reply", "reason", "confidence"],
                },
            })
        return tools

    def _execute_email_agent_tool(
        self,
        name: str,
        tool_input: dict,
        *,
        email: dict,
        ctx: "RunContext | None",
        sent: list[dict],
        drafted: list[dict],
        skipped: list[dict],
    ) -> str:
        uid, sender, subject = email["uid"], email["sender"], email["subject"]
        effects = ctx.effects if ctx else None

        if name == "skip_email":
            reason = tool_input.get("reason", "")
            success_item = {
                "uid": uid,
                "subject": subject,
                "sender": sender,
                "reason": reason,
                "summary": tool_input.get("summary", ""),
            }
            if effects is not None:
                effects.add(
                    "mark_read",
                    {"uid": uid},
                    {
                        "success_bucket": "skipped",
                        "success_item": success_item,
                        "failure_item": dict(success_item),
                    },
                    idempotency_key=self._effect_key(uid, "agent_skip_mark_read"),
                )
            else:
                skipped.append(success_item)
            return f"已登记跳过邮件：{subject}"

        if name == "save_draft":
            reply = tool_input.get("reply", "")
            reason = tool_input.get("reason", "")
            success_item = {
                "uid": uid,
                "subject": subject,
                "sender": sender,
                "reason": reason,
            }
            failure_item = dict(success_item)
            if effects is not None:
                effects.add(
                    "save_draft_and_mark_read",
                    {"uid": uid, "to": sender, "subject": subject, "body": reply},
                    {
                        "success_bucket": "drafted",
                        "success_item": success_item,
                        "failure_item": failure_item,
                    },
                    idempotency_key=self._effect_key(uid, "agent_save_draft"),
                )
            else:
                drafted.append(success_item)
            return f"已登记保存草稿：{subject}"

        if name == "send_reply":
            confidence = int(tool_input.get("confidence", 0) or 0)
            if confidence < AUTO_SEND_CONFIDENCE:
                return self._execute_email_agent_tool(
                    "save_draft",
                    {
                        "reply": tool_input.get("reply", ""),
                        "reason": f"置信度不足，已改存草稿：{tool_input.get('reason', '')}",
                        "confidence": confidence,
                    },
                    email=email,
                    ctx=ctx,
                    sent=sent,
                    drafted=drafted,
                    skipped=skipped,
                )
            reply = tool_input.get("reply", "")
            success_item = {
                "uid": uid,
                "subject": subject,
                "sender": sender,
                "reply": reply,
            }
            failure_item = {
                "uid": uid,
                "subject": subject,
                "sender": sender,
            }
            if effects is not None:
                effects.add(
                    "send_email_and_mark_read",
                    {"uid": uid, "to": sender, "subject": subject, "body": reply},
                    {
                        "success_bucket": "sent",
                        "success_item": success_item,
                        "failure_item": failure_item,
                    },
                    idempotency_key=self._effect_key(uid, "agent_send_reply"),
                )
            else:
                sent.append(success_item)
            return f"已登记发送回复：{subject}"

        raise ValueError(f"未知邮件 agent tool: {name}")

    def verify(self, result: dict) -> tuple[bool, str]:
        failed = result.get("failed", [])
        if not failed:
            return True, ""
        subjects = ", ".join(f["subject"] for f in failed)
        return False, f"以下邮件处理失败：{subjects}"

    def fix(self, result: dict, issues: str, ctx: "RunContext | None" = None) -> dict:
        effects = ctx.effects if ctx else None
        repaired = []
        still_failed = []
        for item in result.get("failed", []):
            try:
                fallback = f"[自动回复失败，请人工处理]\n主题：{item['subject']}\n错误：{item['error']}"
                if effects is not None:
                    effects.add(
                        "save_draft_and_mark_read",
                        {
                            "uid": item["uid"],
                            "to": item.get("sender", ""),
                            "subject": item["subject"],
                            "body": fallback,
                        },
                        {
                            "success_bucket": "drafted",
                            "success_item": {
                                "uid": item["uid"],
                                "subject": item["subject"],
                                "sender": item.get("sender", ""),
                                "reason": "处理失败，已存草稿",
                            },
                            "failure_item": {
                                "uid": item["uid"],
                                "subject": item["subject"],
                                "sender": item.get("sender", ""),
                                "reason": "兜底草稿失败",
                            },
                        },
                        idempotency_key=self._effect_key(item["uid"], "fallback_draft"),
                    )
                    repaired.append(item["uid"])
                else:
                    still_failed.append(item)
            except Exception as e:
                log.error(f"兜底草稿失败: {e}")
                still_failed.append({
                    "uid": item["uid"],
                    "subject": item["subject"],
                    "sender": item.get("sender", ""),
                    "error": str(e),
                })
        result["failed"] = still_failed
        if repaired:
            log.info(f"已为失败邮件生成兜底草稿 effect: {len(repaired)} 封")
        return result

    def after_effects(self, result: dict, ctx: "RunContext | None" = None) -> dict:
        if not ctx or not ctx.tools.imap:
            return result
        try:
            result["unread_count"] = ctx.tools.imap.count_unseen()
        except Exception as e:
            log.warning(f"统计未读失败: {e}")
        return result

    def report(self, result: dict) -> str:
        from datetime import datetime
        sent    = result.get("sent", [])
        drafted = result.get("drafted", [])
        skipped = result.get("skipped", [])
        failed  = result.get("failed", [])
        now     = datetime.now().strftime("%m-%d %H:%M")

        lines = [f"📬 邮件处理报告  {now}", ""]
        lines.append(f"✅ 已回复 {len(sent)} 封  |  📝 草稿 {len(drafted)} 封  |  ⏭ 跳过 {len(skipped)} 封  |  ❌ 失败 {len(failed)} 封")

        if sent:
            lines += ["", "━━━━━━━━━━━━━━━━━━", f"✅ 已自动回复 {len(sent)} 封", "━━━━━━━━━━━━━━━━━━"]
            for i, em in enumerate(sent, 1):
                lines.append(f"{i}. {em['subject']}")
                lines.append(f"   👤 {em.get('sender', '')}")
                reply = em.get("reply", "")
                if reply:
                    reply_lines = [l for l in reply.splitlines() if l.strip()][:3]
                    lines.append(f"   💬 {reply_lines[0]}")
                    for rl in reply_lines[1:]:
                        lines.append(f"      {rl}")

        if drafted:
            lines += ["", "━━━━━━━━━━━━━━━━━━", f"📝 存草稿 {len(drafted)} 封（待确认）", "━━━━━━━━━━━━━━━━━━"]
            for i, em in enumerate(drafted, 1):
                lines.append(f"{i}. {em['subject']}")
                lines.append(f"   👤 {em.get('sender', '')}")
                if em.get("reason"):
                    lines.append(f"   💬 {em['reason']}")

        if skipped:
            lines += ["", "━━━━━━━━━━━━━━━━━━", f"⏭ 无需回复 {len(skipped)} 封", "━━━━━━━━━━━━━━━━━━"]
            for i, em in enumerate(skipped, 1):
                lines.append(f"{i}. {em['subject']}")
                lines.append(f"   👤 {em.get('sender', '')}")
                content_summary = em.get("summary") or em.get("reason", "")
                if content_summary:
                    lines.append(f"   📄 {content_summary}")

        if failed:
            lines += ["", "━━━━━━━━━━━━━━━━━━", f"❌ 失败 {len(failed)} 封", "━━━━━━━━━━━━━━━━━━"]
            for i, em in enumerate(failed, 1):
                lines.append(f"{i}. {em['subject']}")
                lines.append(f"   ⚠️ {em.get('error', '')}")

        plain = f"邮件处理完成：{len(sent)} 封已回复，{len(drafted)} 封存草稿，{len(skipped)} 封跳过，{len(failed)} 封失败"
        result["notification_text"] = "\n".join(lines)
        log.info(plain)
        return plain

    def build_notifications(
        self,
        result: dict,
        summary: str,
        ctx: "RunContext | None" = None,
    ) -> list[dict]:
        return [{
            "channel": "telegram_message",
            "text": result.get("notification_text", summary),
            "parse_mode": "HTML",
        }]
