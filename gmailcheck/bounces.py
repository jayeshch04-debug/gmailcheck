"""Detect bounce notifications (deleted/disabled accounts) in the sender mailbox."""

from __future__ import annotations

import email
import email.policy
import imaplib
import re
from dataclasses import dataclass
from datetime import datetime
from email.message import Message

from . import imap
from .checker import extract_tokens

_ADDR_RE = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


@dataclass
class Bounce:
    tokens: set[str]
    recipients: set[str]
    reason: str


def _text(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        # Container parts such as message/delivery-status or message/rfc822.
        inner = part.get_payload()
        if isinstance(inner, list):
            return "\n".join(m.as_string() for m in inner if isinstance(m, Message))
        return str(inner or "")
    return payload.decode(part.get_content_charset() or "utf-8", "replace")


def parse_bounce(raw: bytes) -> Bounce | None:
    """Parse a DSN / bounce message. Returns None for delay-only notices."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    subject = str(msg.get("Subject", ""))

    whole = raw.decode("utf-8", "replace")
    tokens = extract_tokens(whole)

    recipients: set[str] = set()
    for value in msg.get_all("X-Failed-Recipients", []):
        recipients.update(a.lower() for a in _ADDR_RE.findall(value))

    actions: list[str] = []
    diagnostic = status = ""
    for part in msg.walk():
        if part.get_content_type() != "message/delivery-status":
            continue
        status_text = _text(part)
        for line in status_text.splitlines():
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key in ("final-recipient", "original-recipient"):
                recipients.update(a.lower() for a in _ADDR_RE.findall(value))
            elif key == "action":
                actions.append(value.lower())
            elif key == "diagnostic-code" and not diagnostic:
                diagnostic = value
            elif key == "status" and not status:
                status = f"status {value}"
    reason = diagnostic or status

    if actions and all(a.startswith("delayed") for a in actions):
        return None
    if not actions and re.search(r"\bdelay", subject, re.I):
        return None
    if not tokens and not recipients:
        return None
    return Bounce(tokens=tokens, recipients=recipients, reason=reason or subject or "bounced")


def scan_bounces(conn: imaplib.IMAP4, since: datetime,
                 seen: set[bytes] | None = None) -> list[Bounce]:
    """Parse bounces since `since`. UIDs in `seen` are skipped, and new ones added to it."""
    roles = imap.special_folders(conn)
    mailbox = roles.get("\\All", "INBOX")
    if not imap.select(conn, mailbox, readonly=True):
        return []
    uids = imap.uid_search(conn, "SINCE", imap.search_since(since),
                           "OR", "FROM", '"mailer-daemon"', "FROM", '"postmaster"')
    if seen is not None:
        uids = [u for u in uids if u not in seen]
        seen.update(uids)
    bounces = []
    for _uid, raw in imap.uid_fetch(conn, uids, "(BODY.PEEK[])", batch=50):
        bounce = parse_bounce(raw)
        if bounce:
            bounces.append(bounce)
    return bounces
