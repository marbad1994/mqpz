import logging
import os
import smtplib
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

log = logging.getLogger("mailer")


class SmtpMailer:
    def __init__(self):
        self.host = os.environ["SMTP_HOST"]
        self.port = int(os.environ.get("SMTP_PORT", "587"))
        self.user = os.environ["SMTP_USER"]
        self.password = os.environ["SMTP_PASS"]
        self.from_addr = os.environ.get("FROM_ADDR", self.user)
        self.from_name = os.environ.get("FROM_NAME", "The Game Master")
        self.dry = os.environ.get("MAIL_DRY_RUN") == "1"

    def send(self, to, subject, body, in_reply_to=None):
        msg = EmailMessage()
        msg["From"] = f"{self.from_name} <{self.from_addr}>"
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=self.from_addr.split("@")[-1])
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
            msg["References"] = in_reply_to
        msg.set_content(body)
        if self.dry:
            log.warning("DRY RUN to=%s subject=%s\n%s", to, subject, body)
            return
        if self.port == 465:
            s = smtplib.SMTP_SSL(self.host, self.port, timeout=30)
        else:
            s = smtplib.SMTP(self.host, self.port, timeout=30)
            s.starttls()
        try:
            s.login(self.user, self.password)
            s.send_message(msg)
        finally:
            try:
                s.quit()
            except Exception:
                pass
