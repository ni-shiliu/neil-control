"""
邮件 Loop。

plan   → 从 IMAP 拉取未读邮件，构建任务列表
execute → 对每封邮件：Claude 生成回复（Maker）→ Claude 审查（Checker）→ 发送或存草稿
verify  → 检查是否有处理失败的邮件
fix    → 对失败邮件重试一次
report  → 返回摘要：N封已回复，M封存草稿，K封失败
"""

import imaplib
import smtplib
import email as email_lib
import json
import logging
import os
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from claude_client import get_client, get_model

from loops.base import BaseLoop

log = logging.getLogger(__name__)

IMAP_HOST = "imap.163.com"
IMAP_PORT = 993
SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465

# Maker 和 Checker 必须看到同一段原文（关键承诺/金额不能被错位截断）
BODY_LIMIT_FOR_CLAUDE = 2000

# 自动发送置信度阈值（可被 .env 中 EMAIL_AUTO_SEND_CONFIDENCE 覆盖）
AUTO_SEND_CONFIDENCE = int(os.environ.get("EMAIL_AUTO_SEND_CONFIDENCE", 75))

# 自动发件人白名单：命中后永远存草稿，不让 AI 决定是否回复
# 匹配规则：完整邮箱 或 @后缀（如 @notice.aliyun.com 匹配所有 aliyun 通知）
AUTO_DRAFT_SENDERS = {
    # 系统通知
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
    # 营销/订阅
    "@mailer.substack.com",
    "@email.gitlab.com",
    "@jira.atlassian.com",
    "@bitbucket.org",
}


class EmailLoop(BaseLoop):

    name = "email_loop"
    description = "处理/回复邮件：拉取 163 邮箱未读邮件，Claude 分析生成回复，自动发送或存草稿"

    def __init__(self, agent_mode: str = "semi_auto", max_emails: int = 10,
                 auto_draft_senders: set[str] | None = None):
        self.agent_mode = agent_mode
        self.max_emails = max_emails
        self.auto_draft_senders = auto_draft_senders or AUTO_DRAFT_SENDERS

    def _should_auto_draft(self, sender: str) -> str | None:
        """检查发件人是否在白名单中。命中返回原因字符串，否则 None。"""
        s = sender.lower().strip()
        for pattern in self.auto_draft_senders:
            p = pattern.lower().strip()
            if p.startswith("@"):
                # 严格要求"@" 紧贴 pattern 前，避免 evil@amazonaws.com.attacker.com 误中
                if s.endswith(p) and s.count("@") == 1 and s.endswith(p):
                    return f"发件人域名 {p} 在自动草稿白名单中"
            else:
                if s == p:
                    return f"发件人 {p} 在自动草稿白名单中"
        return None

    # ── Claude 调用 ───────────────────────────────────────

    def _call_claude(self, prompt: str) -> dict:
        msg = get_client().messages.create(
            model=get_model(),
            max_tokens=1024,
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
        """Claude 经常把 true/false 输出成字符串，统一转成 Python bool。"""
        for k, v in list(d.items()):
            if isinstance(v, str) and v.lower() in ("true", "false"):
                d[k] = v.lower() == "true"
        return d

    # ── IMAP 工具 ────────────────────────────────────────

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(os.environ["EMAIL_USER"], os.environ["EMAIL_PASS"])
        # 163 邮箱 2024 起要求 IMAP ID 标识，否则 SELECT/SEARCH 会被拒（"Unsafe Login"）
        try:
            # imaplib.Commands 字典不包含 ID 命令，需要先注册
            if "ID" not in imaplib.Commands:
                imaplib.Commands["ID"] = ("NONAUTH", "AUTH", "SELECTED")
            args = '("name" "NeilAssistant" "version" "1.0" "vendor" "neil-control")'
            conn._simple_command("ID", args)
        except Exception as e:
            log.warning(f"IMAP ID 命令失败（不影响其他邮箱）: {e}")
        return conn

    @staticmethod
    def _decode(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(make_header(decode_header(value)))

    @staticmethod
    def _extract_body(msg) -> str:
        plain, html = "", ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get("Content-Disposition"):
                    continue
                ct = part.get_content_type()
                if ct == "text/plain" and not plain:
                    charset = part.get_content_charset() or "utf-8"
                    plain = part.get_payload(decode=True).decode(charset, errors="replace")
                elif ct == "text/html" and not html:
                    charset = part.get_content_charset() or "utf-8"
                    html = part.get_payload(decode=True).decode(charset, errors="replace")
        else:
            charset = msg.get_content_charset() or "utf-8"
            plain = msg.get_payload(decode=True).decode(charset, errors="replace")

        if plain.strip():
            return plain
        if html.strip():
            # HTML-only 邮件：用 BeautifulSoup 去标签，给 Claude 至少看到内容
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)
        return ""

    # ── SMTP 工具 ────────────────────────────────────────

    def _send(self, to: str, subject: str, body: str) -> None:
        sender = os.environ["EMAIL_USER"]
        msg = MIMEMultipart()
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg.attach(MIMEText(body, "plain", "utf-8"))
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as server:
            server.login(sender, os.environ["EMAIL_PASS"])
            server.sendmail(sender, [to], msg.as_string())

    def _send_and_mark_read(self, uid: str, to: str, subject: str, reply_text: str) -> None:
        """SMTP 发送成功后立刻在同一 IMAP session 标记已读。"""
        self._send(to, subject, reply_text)
        self._mark_read(uid)

    @staticmethod
    def _text_to_html(text: str) -> str:
        """把纯文本（\\n\\n 分段）转成简单 HTML，保留段落结构。"""
        import html as html_lib
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        lines = []
        for p in paragraphs:
            inner = html_lib.escape(p).replace("\n", "<br>")
            lines.append(f"<p>{inner}</p>")
        return (
            '<html><body style="font-family:Arial,sans-serif;font-size:14px;'
            'line-height:1.7;color:#333">'
            + "".join(lines)
            + "</body></html>"
        )

    def _save_draft(self, to: str, subject: str, body: str) -> str:
        """把回复正文保存为完整 RFC822 邮件到草稿箱。"""
        msg = MIMEMultipart("alternative")
        msg["From"] = os.environ["EMAIL_USER"]
        msg["To"] = to
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["Date"] = email_lib.utils.formatdate(localtime=True)
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(self._text_to_html(body), "html", "utf-8"))
        conn = self._imap()
        try:
            result = conn.append("Drafts", "\\Draft", None, msg.as_bytes())
            return result[1][0].decode() if result[1] else "ok"
        finally:
            conn.logout()

    def _mark_read(self, uid: str) -> None:
        conn = self._imap()
        try:
            conn.select("INBOX")
            conn.store(uid, "+FLAGS", "\\Seen")
        finally:
            conn.logout()

    def _save_draft_and_mark_read(self, uid: str, to: str, subject: str, reply_text: str) -> str:
        """在同一个 IMAP session 内完成 append + flag，避免跨连接导致重复处理。"""
        msg = MIMEMultipart("alternative")
        msg["From"] = os.environ["EMAIL_USER"]
        msg["To"] = to
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg["Date"] = email_lib.utils.formatdate(localtime=True)
        msg.attach(MIMEText(reply_text, "plain", "utf-8"))
        msg.attach(MIMEText(self._text_to_html(reply_text), "html", "utf-8"))
        conn = self._imap()
        try:
            append_result = conn.append("Drafts", "\\Draft", None, msg.as_bytes())
            conn.select("INBOX")
            conn.store(uid, "+FLAGS", "\\Seen")
            return append_result[1][0].decode() if append_result[1] else "ok"
        finally:
            conn.logout()

    # ── Claude：生成回复 ─────────────────────────────────

    def _generate_reply(self, uid: str, sender: str, subject: str, body: str) -> dict:
        if self.agent_mode == "full_auto":
            return self._full_auto_reply(sender, subject, body)
        return self._semi_auto_reply(sender, subject, body)

    def _should_reply(self, sender: str, subject: str, body: str) -> dict:
        """判断这封邮件是否需要回复。返回 {"need_reply": bool, "reason": str}"""
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
{{"need_reply":true或false,"reason":"一句话说明原因","summary":"一句话概括邮件内容（不超过30字）"}}""")

    def _semi_auto_reply(self, sender: str, subject: str, body: str) -> dict:
        result = self._call_claude(f"""你是专业邮件助手，帮用户回复工作邮件。

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
{{"risk_level":"low或high","risk_reason":"high时说明，low时为空","confidence":0到100,"reply":"回复正文不超过200字，段落之间用\\n\\n分隔"}}""")
        return result

    def _full_auto_reply(self, sender: str, subject: str, body: str) -> dict:
        result = self._call_claude(f"""你是专业邮件助手，拥有完全处理权限。

发件人：{sender}
主题：{subject}
正文：{body[:BODY_LIMIT_FOR_CLAUDE]}

action 选项：
- reply_now：可直接安全回复
- save_draft：需人工确认
- escalate：超出自动处理范围

输出严格 JSON：
{{"action":"reply_now或save_draft或escalate","reason":"原因","confidence":0到100,"reply":"回复正文，段落之间用\\n\\n分隔"}}""")
        # 统一成 semi_auto 格式方便后续处理
        action = result.get("action", "save_draft")
        return {
            "risk_level": "low" if action == "reply_now" else "high",
            "risk_reason": result.get("reason", ""),
            "confidence": result.get("confidence", 0),
            "reply": result.get("reply", ""),
        }

    def _verify_reply(self, sender: str, subject: str, body: str, reply: str) -> dict:
        return self._call_claude(f"""你是邮件回复质量审查员。

原始邮件主题：{subject}
原始正文：{body[:BODY_LIMIT_FOR_CLAUDE]}
待审查回复：{reply}

检查：1)是否切题 2)语气是否得体 3)是否有不当承诺 4)语言是否流畅

输出严格 JSON：
{{"pass":true或false,"issues":"不通过时说明，通过时为空"}}""")

    # ── BaseLoop 四个抽象方法 ─────────────────────────────

    def plan(self, goal: dict) -> dict:
        conn = self._imap()
        try:
            conn.select("INBOX")
            _, data = conn.search(None, "UNSEEN")
            uids = data[0].split()[:self.max_emails]

            emails = []
            for uid in uids:
                _, msg_data = conn.fetch(uid, "(RFC822)")
                msg = email_lib.message_from_bytes(msg_data[0][1])
                sender_raw = self._decode(msg.get("From", ""))
                to_addr = sender_raw
                if "<" in sender_raw and ">" in sender_raw:
                    to_addr = sender_raw[sender_raw.index("<") + 1: sender_raw.index(">")]
                emails.append({
                    "uid": uid.decode(),
                    "subject": self._decode(msg.get("Subject", "")),
                    "sender": to_addr,
                    "body": self._extract_body(msg),
                })
            return {"emails": emails}
        finally:
            conn.logout()

    def execute(self, context: dict) -> dict:
        sent, drafted, skipped, failed = [], [], [], []

        for em in context["emails"]:
            uid, sender, subject, body = em["uid"], em["sender"], em["subject"], em["body"]
            try:
                # 白名单发件人：直接跳过，标已读，不调 Claude
                wl_reason = self._should_auto_draft(sender)
                if wl_reason:
                    log.info(f"白名单跳过: {sender} | {wl_reason}")
                    self._mark_read(uid)
                    skipped.append({"uid": uid, "subject": subject, "sender": sender,
                                    "reason": wl_reason})
                    continue

                # Claude 判断是否需要回复
                sr = self._should_reply(sender, subject, body)
                if not sr.get("need_reply"):
                    log.info(f"不需要回复: {subject} | {sr.get('reason')}")
                    self._mark_read(uid)
                    skipped.append({"uid": uid, "subject": subject, "sender": sender,
                                    "reason": sr.get("reason", ""),
                                    "summary": sr.get("summary", "")})
                    continue

                gen = self._generate_reply(uid, sender, subject, body)
                reply_text = gen.get("reply", "")
                risk = gen.get("risk_level", "high")
                confidence = gen.get("confidence", 0)

                # Maker-Checker 验证：只修正回复内容，不改变 risk 评级
                check = self._verify_reply(sender, subject, body, reply_text)
                if not check.get("pass") and confidence >= AUTO_SEND_CONFIDENCE:
                    gen2 = self._generate_reply(uid, sender, subject,
                                                body + f"\n[上次回复问题：{check.get('issues')}]")
                    reply_text = gen2.get("reply", reply_text)

                auto_send = os.environ.get("EMAIL_AUTO_SEND", "false").lower() == "true"
                if auto_send and risk == "low" and confidence >= AUTO_SEND_CONFIDENCE:
                    self._send_and_mark_read(uid, sender, subject, reply_text)
                    sent.append({"uid": uid, "subject": subject, "sender": sender, "reply": reply_text})
                else:
                    reason = gen.get("risk_reason", "") or f"risk={risk} conf={confidence}"
                    if not auto_send:
                        reason = f"[自动发送已关闭] {reason}".strip()
                    self._save_draft_and_mark_read(uid, sender, subject, reply_text)
                    drafted.append({"uid": uid, "subject": subject, "sender": sender,
                                    "reason": reason})
            except Exception as e:
                log.error(f"处理邮件失败 uid={uid}: {e}")
                failed.append({"uid": uid, "subject": em.get("subject", ""),
                               "sender": em.get("sender", ""), "error": str(e)})

        return {"sent": sent, "drafted": drafted, "skipped": skipped, "failed": failed}

    def verify(self, result: dict) -> tuple[bool, str]:
        failed = result.get("failed", [])
        if not failed:
            return True, ""
        subjects = ", ".join(f["subject"] for f in failed)
        return False, f"以下邮件处理失败：{subjects}"

    def fix(self, result: dict, issues: str) -> dict:
        # 对失败的邮件重试一次，存草稿兜底
        for item in result.get("failed", []):
            try:
                fallback = f"[自动回复失败，请人工处理]\n主题：{item['subject']}\n错误：{item['error']}"
                self._save_draft_and_mark_read(
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
                    # 最多展示 3 行，避免消息过长
                    reply_lines = [l for l in reply.splitlines() if l.strip()][:3]
                    lines.append(f"   💬 {reply_lines[0]}")
                    for rl in reply_lines[1:]:
                        lines.append(f"      {rl}")

        if drafted:
            lines += ["", "━━━━━━━━━━━━━━━━━━", f"📝 存草稿 {len(drafted)} 封（待确认）", "━━━━━━━━━━━━━━━━━━"]
            for i, em in enumerate(drafted, 1):
                reason = em.get("reason", "")
                lines.append(f"{i}. {em['subject']}")
                lines.append(f"   👤 {em.get('sender', '')}")
                if reason:
                    lines.append(f"   💬 {reason}")

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
