"""Send the tagged test emails over SMTP."""

from __future__ import annotations

import random
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Callable

from .config import SmtpConfig

TOKEN_HEADER = "X-GmailCheck-Token"


def build_message(sender: str, to: str, token: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = f"Forward check {token}"
    msg["Date"] = formatdate(localtime=True)
    domain = sender.rsplit("@", 1)[-1] if "@" in sender else None
    msg["Message-ID"] = make_msgid(idstring=token, domain=domain)
    msg[TOKEN_HEADER] = token
    msg.set_content(
        f"Hi,\n\nThis is an automated check that mail sent to {to} is still being "
        f"forwarded.\n\nReference: {token}\n\nNo reply needed.\n",
        cte="7bit",
    )
    return msg


class SmtpSender:
    """Holds one SMTP connection open and reconnects when it drops."""

    def __init__(self, cfg: SmtpConfig, connect: Callable[[SmtpConfig], smtplib.SMTP] | None = None):
        self.cfg = cfg
        self._connect_fn = connect or _connect
        self._conn: smtplib.SMTP | None = None

    def _ensure(self) -> smtplib.SMTP:
        if self._conn is None:
            self._conn = self._connect_fn(self.cfg)
        return self._conn

    def send(self, msg: EmailMessage) -> None:
        """Send one message. Raises smtplib.SMTPRecipientsRefused on a hard reject."""
        for attempt in range(3):
            try:
                self._ensure().send_message(msg)
                return
            except (smtplib.SMTPServerDisconnected, ConnectionError, TimeoutError):
                self._drop()
                if attempt == 2:
                    raise
                time.sleep(2 * (attempt + 1))

    def _drop(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
        self._conn = None

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except Exception:
                pass
        self._conn = None

    def __enter__(self) -> "SmtpSender":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _connect(cfg: SmtpConfig) -> smtplib.SMTP:
    context = ssl.create_default_context()
    if cfg.security == "ssl":
        conn: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, context=context, timeout=60)
    else:
        conn = smtplib.SMTP(cfg.host, cfg.port, timeout=60)
        conn.ehlo()
        if cfg.security == "starttls":
            conn.starttls(context=context)
            conn.ehlo()
    conn.login(cfg.user, cfg.password)
    return conn


def throttle(delay: float) -> None:
    if delay > 0:
        time.sleep(delay + random.uniform(0, delay * 0.5))
