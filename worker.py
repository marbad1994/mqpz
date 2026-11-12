"""Background threads: IMAP IDLE (realtime inbound) and the scheduler tick."""
import email
import html
import logging
import os
import re
import threading
import time
from email import policy
from email.utils import parseaddr

from imapclient import IMAPClient

log = logging.getLogger("worker")

QUOTE_HEAD = re.compile(r"^(on .+wrote:|la .+a scris:|-{2,}\s*original message|from:\s.+@)", re.I)


def clean_body(text):
    out = []
    lines = text.replace("\r", "").split("\n")
    for i, line in enumerate(lines):
        joined = (line + " " + (lines[i + 1] if i + 1 < len(lines) else "")).strip()
        if line.lstrip().startswith(">") or QUOTE_HEAD.match(line.strip()) or QUOTE_HEAD.match(joined):
            break
        out.append(line)
    return "\n".join(out).strip()


def body_of(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    text = part.get_content()
    if part.get_content_type() == "text/html":
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return text


class Workers:
    def __init__(self, game):
        self.game = game
        self.host = os.environ["IMAP_HOST"]
        self.user = os.environ["IMAP_USER"]
        self.password = os.environ["IMAP_PASS"]
        self.me = os.environ.get("FROM_ADDR", self.user).lower()
        self.last_uid = None

    def start(self):
        threading.Thread(target=self._idle_loop, daemon=True, name="imap").start()
        threading.Thread(target=self._tick_loop, daemon=True, name="tick").start()

    def _tick_loop(self):
        while True:
            try:
                self.game.tick()
            except Exception:
                log.exception("tick failed")
            time.sleep(10)

    def _idle_loop(self):
        backoff = 5
        while True:
            try:
                with IMAPClient(self.host, ssl=True, timeout=60) as c:
                    c.login(self.user, self.password)
                    c.select_folder("INBOX")
                    if self.last_uid is None:
                        uids = c.search(["ALL"])
                        self.last_uid = max(uids) if uids else 0
                    backoff = 5
                    while True:
                        self._fetch_new(c)
                        c.idle()
                        c.idle_check(timeout=20 * 60)
                        c.idle_done()
            except Exception:
                log.exception("imap loop failed, retrying in %ss", backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 120)

    def _fetch_new(self, c):
        uids = [u for u in c.search(["UID", f"{self.last_uid + 1}:*"]) if u > self.last_uid]
        if not uids:
            return
        for uid, data in c.fetch(uids, ["RFC822"]).items():
            self.last_uid = max(self.last_uid, uid)
            try:
                self._handle(email.message_from_bytes(data[b"RFC822"], policy=policy.default))
            except Exception:
                log.exception("failed to handle uid %s", uid)

    def _handle(self, msg):
        sender = parseaddr(msg.get("From", ""))[1].lower()
        if sender != self.game.player().lower() or sender == self.me:
            self.game.log("ignored", f"mail from {sender}")
            return
        if msg.get("Auto-Submitted", "no").lower() != "no" or "bulk" in msg.get("Precedence", "").lower():
            return
        text = clean_body(body_of(msg))
        self.game.handle_inbound(text, msg.get("Message-ID"))
        self.game.tick()  # flush the reply immediately
