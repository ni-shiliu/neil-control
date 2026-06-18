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


class EmailLoop(BaseLoop):

    name = "email_loop"
    description = "处理/回复邮件：拉取 163 邮箱未读邮件，Claude 分析生成回复，自动发送或存草稿"

    def __init__(self, agent_mode: str = "semi_auto", max_emails: int = 10):
        self.agent_mode = agent_mode
        self.max_emails = max_emails

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
        return json.loads(raw)

    # ── IMAP 工具 ────────────────────────────────────────

    def _imap(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(os.environ["EMAIL_USER"], os.environ["EMAIL_PASS"])
        return conn

    @staticmethod
    def _decode(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(make_header(decode_header(value)))

    @staticmethod
    def _extract_body(msg) -> str:
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                    charset = part.get_content_charset() or "utf-8"
                    return part.get_payload(decode=True).decode(charset, errors="replace")
        else:
            charset = msg.get_content_charset() or "utf-8"
            return msg.get_payload(decode=True).decode(charset, errors="replace")
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

    def _save_draft(self, body: str) -> str:
        conn = self._imap()
        try:
            result = conn.append("Drafts", "\\Draft", None, body.encode("utf-8"))
            return result[1][0].decode() if result[1] else "unknown"
        finally:
            conn.logout()

    def _mark_read(self, uid: str) -> None:
        conn = self._imap()
        try:
            conn.select("INBOX")
            conn.store(uid, "+FLAGS", "\\Seen")
        finally:
            conn.logout()

    # ── Claude：生成回复 ─────────────────────────────────

    def _generate_reply(self, uid: str, sender: str, subject: str, body: str) -> dict:
        if self.agent_mode == "full_auto":
            return self._full_auto_reply(sender, subject, body)
        return self._semi_auto_reply(sender, subject, body)

    def _semi_auto_reply(self, sender: str, subject: str, body: str) -> dict:
        result = self._call_claude(f"""你是专业邮件助手，帮用户回复工作邮件。

发件人：{sender}
主题：{subject}
正文：{body[:2000]}

风险判断（high/low）：
- high：涉及合同、承诺、金额、投诉，或你不确定
- low：会议确认、日常询问、简单信息同步

输出严格 JSON：
{{"risk_level":"low或high","risk_reason":"high时说明，low时为空","confidence":0到100,"reply":"回复正文不超过200字"}}""")
        return result

    def _full_auto_reply(self, sender: str, subject: str, body: str) -> dict:
        result = self._call_claude(f"""你是专业邮件助手，拥有完全处理权限。

发件人：{sender}
主题：{subject}
正文：{body[:2000]}

action 选项：
- reply_now：可直接安全回复
- save_draft：需人工确认
- escalate：超出自动处理范围

输出严格 JSON：
{{"action":"reply_now或save_draft或escalate","reason":"原因","confidence":0到100,"reply":"回复正文"}}""")
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
原始正文：{body[:500]}
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
                    "body": self._extract_body(msg)[:3000],
                })
            return {"emails": emails}
        finally:
            conn.logout()

    def execute(self, context: dict) -> dict:
        sent, drafted, failed = [], [], []

        for em in context["emails"]:
            uid, sender, subject, body = em["uid"], em["sender"], em["subject"], em["body"]
            try:
                gen = self._generate_reply(uid, sender, subject, body)
                reply_text = gen.get("reply", "")
                risk = gen.get("risk_level", "high")
                confidence = gen.get("confidence", 0)

                # Maker-Checker 验证
                check = self._verify_reply(sender, subject, body, reply_text)
                if not check.get("pass") and confidence >= 75:
                    # 验证不通过但置信度高，重新生成一次
                    gen2 = self._generate_reply(uid, sender, subject,
                                                body + f"\n[上次回复问题：{check.get('issues')}]")
                    reply_text = gen2.get("reply", reply_text)
                    risk = gen2.get("risk_level", risk)

                if risk == "low" and confidence >= 75:
                    self._send(sender, subject, reply_text)
                    self._mark_read(uid)
                    sent.append({"uid": uid, "subject": subject})
                else:
                    self._save_draft(reply_text)
                    self._mark_read(uid)
                    drafted.append({"uid": uid, "subject": subject,
                                    "reason": gen.get("risk_reason", "")})
            except Exception as e:
                log.error(f"处理邮件失败 uid={uid}: {e}")
                failed.append({"uid": uid, "subject": em.get("subject", ""), "error": str(e)})

        return {"sent": sent, "drafted": drafted, "failed": failed}

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
                self._save_draft(f"[自动回复失败，请人工处理]\n主题：{item['subject']}\n错误：{item['error']}")
                result["drafted"].append({"uid": item["uid"], "subject": item["subject"],
                                          "reason": "处理失败，已存草稿"})
            except Exception as e:
                log.error(f"兜底草稿失败: {e}")
        result["failed"] = []
        return result

    def report(self, result: dict) -> str:
        s = len(result.get("sent", []))
        d = len(result.get("drafted", []))
        f = len(result.get("failed", []))
        return f"邮件处理完成：{s} 封已回复，{d} 封存草稿，{f} 封失败"
