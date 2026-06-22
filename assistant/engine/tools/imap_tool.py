"""
IMAPTool — 封装 163 IMAP 操作，含 IDLE 事件监听。

163 邮箱特殊处理：
  1. 登录后必须发 IMAP ID 命令，否则 SELECT/SEARCH 被拒（Unsafe Login）
  2. imaplib 不内置 ID 命令，需要手动注册到 Commands 字典
  3. IDLE 需要用 imaplib 的底层 socket 手动发 IDLE/DONE
"""

import email as email_lib
import email.utils
import imaplib
import logging
import os
import threading
import time
from email.header import decode_header, make_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Callable

log = logging.getLogger(__name__)

IMAP_HOST = "imap.163.com"
IMAP_PORT = 993


class IMAPTool:

    def __init__(self, host: str = IMAP_HOST, port: int = IMAP_PORT):
        self.host = host
        self.port = port
        self._idle_thread: threading.Thread | None = None
        self._idle_stop = threading.Event()

    # ── 连接 ─────────────────────────────────────────────

    def _connect(self) -> imaplib.IMAP4_SSL:
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        conn.login(os.environ["EMAIL_USER"], os.environ["EMAIL_PASS"])
        # 163 要求 IMAP ID 命令
        if "ID" not in imaplib.Commands:
            imaplib.Commands["ID"] = ("NONAUTH", "AUTH", "SELECTED")
        try:
            conn._simple_command("ID", '("name" "NeilAssistant" "version" "1.0")')
        except Exception as e:
            log.warning(f"[imap] ID 命令失败（非 163 服务器？）: {e}")
        return conn

    # ── 解码工具 ─────────────────────────────────────────

    @staticmethod
    def _decode(value) -> str:
        if value is None:
            return ""
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
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True)
        return ""

    # ── 读邮件 ───────────────────────────────────────────

    def fetch_unseen(self, limit: int = 10) -> list[dict]:
        """拉取未读邮件，返回 [{uid, subject, sender, body}, ...]。"""
        conn = self._connect()
        try:
            conn.select("INBOX")
            _, data = conn.uid("SEARCH", None, "UNSEEN")
            uids = data[0].split()[:limit]
            emails = []
            for uid in uids:
                _, msg_data = conn.uid("FETCH", uid, "(RFC822)")
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
            return emails
        finally:
            conn.logout()

    def count_unseen(self) -> int:
        """返回收件箱未读邮件数量。"""
        conn = self._connect()
        try:
            conn.select("INBOX")
            _, data = conn.uid("SEARCH", None, "UNSEEN")
            return len(data[0].split()) if data[0] else 0
        finally:
            conn.logout()

    # ── 写操作 ───────────────────────────────────────────

    def mark_read(self, uid: str) -> None:
        conn = self._connect()
        try:
            conn.select("INBOX")
            conn.uid("STORE", uid, "+FLAGS", "\\Seen")
        finally:
            conn.logout()

    def save_draft(self, to: str, subject: str, body: str) -> None:
        """保存草稿（完整 RFC822 邮件）。"""
        from engine.tools.smtp_tool import SMTPTool
        sender = os.environ["EMAIL_USER"]
        subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg = MIMEMultipart("alternative")
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subj
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(SMTPTool._text_to_html(body), "html", "utf-8"))

        conn = self._connect()
        try:
            conn.append("Drafts", "\\Draft", None, msg.as_bytes())
        finally:
            conn.logout()

    def save_draft_and_mark_read(self, uid: str, to: str, subject: str, body: str) -> None:
        """在同一个 IMAP session 内完成 append + flag，避免跨连接重复处理。"""
        from engine.tools.smtp_tool import SMTPTool
        sender = os.environ["EMAIL_USER"]
        subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg = MIMEMultipart("alternative")
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subj
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(SMTPTool._text_to_html(body), "html", "utf-8"))

        conn = self._connect()
        try:
            conn.append("Drafts", "\\Draft", None, msg.as_bytes())
            conn.select("INBOX")
            conn.uid("STORE", uid, "+FLAGS", "\\Seen")
        finally:
            conn.logout()

    # ── IMAP IDLE（event 模式）───────────────────────────

    def idle_listen(self, callback: Callable[[], None]) -> None:
        """后台线程持续 IDLE 监听，新邮件到达时调用 callback()。
        使用 IMAP IDLE 命令，163 支持（RFC 2177）。
        """
        if self._idle_thread and self._idle_thread.is_alive():
            log.warning("[imap] IDLE 线程已在运行，跳过重复注册")
            return

        self._idle_stop.clear()
        self._idle_thread = threading.Thread(
            target=self._idle_loop,
            args=(callback,),
            daemon=True,
            name="imap-idle",
        )
        self._idle_thread.start()
        log.info("[imap] IDLE 监听线程已启动")

    def stop_idle(self) -> None:
        self._idle_stop.set()

    def _idle_loop(self, callback: Callable[[], None]) -> None:
        """IDLE 主循环，断线自动重连。"""
        IDLE_TIMEOUT = 1200   # 20分钟，RFC 建议值
        RETRY_DELAY  = 30

        while not self._idle_stop.is_set():
            try:
                conn = self._connect()
                conn.select("INBOX")
                log.info("[imap] 进入 IDLE 状态")

                # 发 IDLE 命令
                tag = conn._new_tag()
                conn.send(tag + b" IDLE\r\n")

                # 等待 "+ idling" 确认
                resp = conn.readline()
                if not resp.startswith(b"+"):
                    log.warning(f"[imap] IDLE 未确认: {resp}")
                    conn.logout()
                    time.sleep(RETRY_DELAY)
                    continue

                deadline = time.time() + IDLE_TIMEOUT
                while not self._idle_stop.is_set() and time.time() < deadline:
                    # 非阻塞检查是否有数据（1 秒超时）
                    conn.sock.settimeout(1.0)
                    try:
                        line = conn.readline()
                        if line:
                            log.debug(f"[imap] IDLE 收到: {line!r}")
                            # EXISTS 表示新邮件到达
                            if b"EXISTS" in line:
                                log.info("[imap] 检测到新邮件，触发 callback")
                                # 先发 DONE 结束 IDLE，再执行 callback
                                conn.send(b"DONE\r\n")
                                conn.readline()  # 读 OK 响应
                                conn.logout()
                                try:
                                    callback()
                                except Exception as e:
                                    log.error(f"[imap] callback 执行失败: {e}")
                                break
                    except (TimeoutError, OSError):
                        pass  # 超时是正常的，继续等

                else:
                    # IDLE_TIMEOUT 到期，发 DONE 重新 IDLE（保持连接活跃）
                    try:
                        conn.send(b"DONE\r\n")
                        conn.readline()
                        conn.logout()
                    except Exception:
                        pass

            except Exception as e:
                log.error(f"[imap] IDLE 循环异常，{RETRY_DELAY}s 后重连: {e}")
                time.sleep(RETRY_DELAY)
