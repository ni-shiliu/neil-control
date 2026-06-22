"""
邮件 Loop。

plan   → 从 IMAP 拉取未读邮件，历史 pattern 从记忆读取
execute → 对每封邮件：白名单/should_reply → Maker-Checker → 发送或存草稿
verify  → 检查是否有处理失败的邮件
fix    → 对失败邮件兜底存草稿
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

    def extract_memory(self, result: dict, old_memory: dict) -> dict:
        """沉淀跳过的发件人 pattern，最多保留 200 条。"""
        patterns = old_memory.get("skip_patterns", [])
        seen = {p["sender"] for p in patterns}
        for s in result.get("skipped", []):
            if s["sender"] not in seen:
                patterns.append({"sender": s["sender"], "reason": s.get("reason", "")})
                seen.add(s["sender"])
        return {
            **old_memory,
            "skip_patterns": patterns[-200:],
            "unread_count": result.get("unread_count", 0),
        }

    # ── 白名单：静态 + 记忆动态 pattern ─────────────────

    def _should_auto_skip(self, sender: str, memory: dict) -> str | None:
        """检查发件人是否应跳过。命中返回原因，否则 None。"""
        s = sender.lower().strip()

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
        memory = ctx.memory if ctx else {}
        if ctx and ctx.tools.imap:
            emails = ctx.tools.imap.fetch_unseen(limit=self.max_emails)
        else:
            # 降级：直接用 IMAPTool（兼容旧测试）
            from engine.tools.imap_tool import IMAPTool
            emails = IMAPTool().fetch_unseen(limit=self.max_emails)
        return {"emails": emails, "memory": memory}

    def execute(self, context: dict, ctx: "RunContext | None" = None) -> dict:
        sent, drafted, skipped, failed = [], [], [], []
        memory = context.get("memory", {})

        imap  = ctx.tools.imap  if ctx and ctx.tools.imap  else None
        smtp  = ctx.tools.smtp  if ctx and ctx.tools.smtp  else None

        for em in context["emails"]:
            uid, sender, subject, body = em["uid"], em["sender"], em["subject"], em["body"]
            try:
                # 白名单 + 历史 pattern 跳过
                skip_reason = self._should_auto_skip(sender, memory)
                if skip_reason:
                    log.info(f"跳过: {sender} | {skip_reason}")
                    if imap:
                        imap.mark_read(uid)
                    skipped.append({"uid": uid, "subject": subject, "sender": sender,
                                    "reason": skip_reason})
                    continue

                # Claude 判断是否需要回复
                sr = self._should_reply(sender, subject, body, ctx)
                if not sr.get("need_reply"):
                    log.info(f"不需要回复: {subject} | {sr.get('reason')}")
                    if imap:
                        imap.mark_read(uid)
                    skipped.append({"uid": uid, "subject": subject, "sender": sender,
                                    "reason": sr.get("reason", ""),
                                    "summary": sr.get("summary", "")})
                    continue

                # Maker：生成回复
                gen = self._generate_reply(uid, sender, subject, body, ctx)
                reply_text = gen.get("reply", "")
                risk = gen.get("risk_level", "high")
                confidence = gen.get("confidence", 0)

                # Checker：验证质量（只修正内容，不改 risk）
                check = self._verify_reply(sender, subject, body, reply_text, ctx)
                if not check.get("pass") and confidence >= AUTO_SEND_CONFIDENCE:
                    gen2 = self._generate_reply(uid, sender, subject,
                                                body + f"\n[上次回复问题：{check.get('issues')}]", ctx)
                    reply_text = gen2.get("reply", reply_text)

                auto_send = os.environ.get("EMAIL_AUTO_SEND", "false").lower() == "true"
                if auto_send and risk == "low" and confidence >= AUTO_SEND_CONFIDENCE:
                    if smtp:
                        smtp.send(sender, subject, reply_text)
                    if imap:
                        imap.mark_read(uid)
                    sent.append({"uid": uid, "subject": subject, "sender": sender,
                                 "reply": reply_text})
                else:
                    reason = gen.get("risk_reason", "") or f"risk={risk} conf={confidence}"
                    if not auto_send:
                        reason = f"[自动发送已关闭] {reason}".strip()
                    if imap:
                        imap.save_draft_and_mark_read(uid, sender, subject, reply_text)
                    drafted.append({"uid": uid, "subject": subject, "sender": sender,
                                    "reason": reason})
            except Exception as e:
                log.error(f"处理邮件失败 uid={uid}: {e}")
                failed.append({"uid": uid, "subject": em.get("subject", ""),
                               "sender": em.get("sender", ""), "error": str(e)})

        # 统计剩余未读（用于 is_goal_met）
        unread_count = 0
        try:
            if imap:
                unread_count = imap.count_unseen()
        except Exception:
            pass

        return {"sent": sent, "drafted": drafted, "skipped": skipped, "failed": failed,
                "unread_count": unread_count}

    def verify(self, result: dict) -> tuple[bool, str]:
        failed = result.get("failed", [])
        if not failed:
            return True, ""
        subjects = ", ".join(f["subject"] for f in failed)
        return False, f"以下邮件处理失败：{subjects}"

    def fix(self, result: dict, issues: str, ctx: "RunContext | None" = None) -> dict:
        imap = ctx.tools.imap if ctx and ctx.tools.imap else None
        for item in result.get("failed", []):
            try:
                fallback = f"[自动回复失败，请人工处理]\n主题：{item['subject']}\n错误：{item['error']}"
                if imap:
                    imap.save_draft_and_mark_read(
                        item["uid"], item.get("sender", ""), item["subject"], fallback
                    )
                result["drafted"].append({"uid": item["uid"], "subject": item["subject"],
                                          "reason": "处理失败，已存草稿"})
            except Exception as e:
                log.error(f"兜底草稿失败: {e}")
        result["failed"] = []
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

        message = "\n".join(lines)
        try:
            import notifier
            notifier.notify_telegram(message, parse_mode="HTML")
        except Exception as e:
            log.warning(f"Telegram 通知失败: {e}")

        plain = f"邮件处理完成：{len(sent)} 封已回复，{len(drafted)} 封存草稿，{len(skipped)} 封跳过，{len(failed)} 封失败"
        log.info(plain)
        return plain
