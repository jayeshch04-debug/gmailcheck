"""Small helpers over imaplib shared by the master checker and bounce scanner."""

from __future__ import annotations

import imaplib
import re
from datetime import datetime, timedelta
from typing import Iterator

from .config import ImapConfig

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\)\s+(?P<delim>"[^"]*"|NIL)\s+(?P<name>.+)$')
_UID_RE = re.compile(rb"UID\s+(\d+)")


class ImapError(Exception):
    pass


def connect(cfg: ImapConfig) -> imaplib.IMAP4:
    try:
        conn = imaplib.IMAP4_SSL(cfg.host, cfg.port, timeout=60)
        conn.login(cfg.user, cfg.password)
    except imaplib.IMAP4.error as exc:
        raise ImapError(f"IMAP login to {cfg.user} failed: {exc}") from exc
    return conn


def imap_date(dt: datetime) -> str:
    """IMAP SEARCH date (DD-Mon-YYYY), locale independent."""
    return f"{dt.day:02d}-{_MONTHS[dt.month - 1]}-{dt.year}"


def search_since(dt: datetime) -> str:
    # SINCE is date-only and uses the server's timezone, so back off a day.
    return imap_date(dt - timedelta(days=1))


def quote(name: str) -> str:
    if name.startswith('"') and name.endswith('"'):
        return name
    return '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'


def special_folders(conn: imaplib.IMAP4) -> dict[str, str]:
    """Map special-use roles (\\All, \\Junk, \\Trash, ...) to quoted mailbox names.

    Works for Gmail regardless of UI language ("[Gmail]" vs "[Google Mail]" etc.).
    """
    typ, data = conn.list()
    if typ != "OK":
        return {}
    roles: dict[str, str] = {}
    for line in data:
        if isinstance(line, tuple):  # literal-form name
            head, name_bytes = line
            match = _LIST_RE.match(head.strip() + b" " + b'"' + name_bytes + b'"')
        elif line:
            match = _LIST_RE.match(line.strip())
        else:
            continue
        if not match:
            continue
        name = match.group("name").decode("utf-8", "replace").strip()
        for flag in match.group("flags").decode().split():
            if flag in ("\\All", "\\Junk", "\\Trash", "\\Sent"):
                roles.setdefault(flag, quote(name))
    return roles


def select(conn: imaplib.IMAP4, mailbox: str, readonly: bool = True) -> bool:
    typ, _ = conn.select(mailbox, readonly=readonly)
    return typ == "OK"


def uid_search(conn: imaplib.IMAP4, *criteria: str) -> list[bytes]:
    typ, data = conn.uid("SEARCH", *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def uid_fetch(conn: imaplib.IMAP4, uids: list[bytes], what: str,
              batch: int = 200) -> Iterator[tuple[bytes, bytes]]:
    """Yield (uid, payload) for each message, fetching in batches."""
    for i in range(0, len(uids), batch):
        chunk = b",".join(uids[i:i + batch]).decode()
        typ, data = conn.uid("FETCH", chunk, what)
        if typ != "OK":
            continue
        for item in data:
            if not isinstance(item, tuple):
                continue
            match = _UID_RE.search(item[0])
            if match:
                yield match.group(1), item[1]


def move_to_trash(conn: imaplib.IMAP4, uids: list[bytes], trash: str) -> None:
    """Move UIDs from the currently selected (read-write) mailbox to trash."""
    if not uids:
        return
    uid_set = b",".join(uids).decode()
    typ, _ = conn.uid("MOVE", uid_set, trash)
    if typ == "OK":
        return
    conn.uid("COPY", uid_set, trash)
    conn.uid("STORE", uid_set, "+FLAGS.SILENT", "(\\Deleted)")
    conn.expunge()


def logout(conn: imaplib.IMAP4) -> None:
    try:
        conn.logout()
    except Exception:
        pass
