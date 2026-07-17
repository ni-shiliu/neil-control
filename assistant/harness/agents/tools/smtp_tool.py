"""SMTPTool — 封装 SMTP 发送邮件。"""

import logging
import os
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import email.utils
import smtplib

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.163.com"
SMTP_PORT = 465
SMTP_LOCAL_HOSTNAME = os.environ.get("SMTP_LOCAL_HOSTNAME", "localhost")


class SMTPTool:

    def __init__(
        self,
        host: str = SMTP_HOST,
        port: int = SMTP_PORT,
        local_hostname: str = SMTP_LOCAL_HOSTNAME,
    ):
        self.host = host
        self.port = port
        self.local_hostname = local_hostname

    @staticmethod
    def _text_to_html(text: str) -> str:
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

    def send(self, to: str, subject: str, body: str) -> None:
        """发送邮件，同时附带 HTML 版本以支持段落格式。"""
        sender = os.environ["EMAIL_USER"]
        subj = subject if subject.lower().startswith("re:") else f"Re: {subject}"

        msg = MIMEMultipart("alternative")
        msg["From"] = sender
        msg["To"] = to
        msg["Subject"] = subj
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg.attach(MIMEText(body, "plain", "utf-8"))
        msg.attach(MIMEText(self._text_to_html(body), "html", "utf-8"))

        with smtplib.SMTP_SSL(
            self.host,
            self.port,
            local_hostname=self.local_hostname,
        ) as server:
            server.login(sender, os.environ["EMAIL_PASS"])
            server.sendmail(sender, [to], msg.as_string())
        log.info(f"[smtp] 已发送至 {to}: {subj}")
