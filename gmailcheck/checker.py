"""Scan the master mailbox for the test emails that were forwarded to it."""

from __future__ import annotations

import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import imap

TOKEN_RE = re.compile(r"GMC-[0-9A-F]{10}")
SUBJECT_PHRASE = "Forward check"
HEADERS = "(BODY.PEEK[HEADER.FIELDS (SUBJECT X-GMAILCHECK-TOKEN)])"


def extract_tokens(payload: bytes | str) -> set[str]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    return set(TOKEN_RE.findall(payload))


@dataclass
class ScanResult:
    found: dict[str, str] = field(default_factory=dict)  # token -> folder label
    uids: dict[str, list[bytes]] = field(default_factory=dict)  # mailbox -> matched UIDs
    folders_scanned: list[str] = field(default_factory=list)


def scan_master(conn: imaplib.IMAP4, since: datetime, known_tokens: set[str]) -> ScanResult:
    """Find known tokens in All Mail and Spam of the master mailbox."""
    roles = imap.special_folders(conn)
    mailboxes = [(roles.get("\\All", "INBOX"), "inbox")]
    if "\\Junk" in roles:
        mailboxes.append((roles["\\Junk"], "spam"))

    result = ScanResult()
    for mailbox, label in mailboxes:
        if not imap.select(conn, mailbox, readonly=True):
            continue
        result.folders_scanned.append(mailbox)
        uids = imap.uid_search(conn, "SINCE", imap.search_since(since),
                               "SUBJECT", f'"{SUBJECT_PHRASE}"')
        for uid, payload in imap.uid_fetch(conn, uids, HEADERS):
            tokens = extract_tokens(payload) & known_tokens
            if not tokens:
                continue
            result.uids.setdefault(mailbox, []).append(uid)
            for token in tokens:
                # A copy in the inbox beats one in spam.
                if result.found.get(token) != "inbox":
                    result.found[token] = label
    return result


def cleanup(conn: imaplib.IMAP4, result: ScanResult) -> int:
    """Move matched test emails to Trash. Returns the number moved."""
    trash = imap.special_folders(conn).get("\\Trash")
    if not trash:
        return 0
    moved = 0
    for mailbox, uids in result.uids.items():
        if imap.select(conn, mailbox, readonly=False):
            imap.move_to_trash(conn, uids, trash)
            moved += len(uids)
    return moved
