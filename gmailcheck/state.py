"""SQLite-backed record of every address, its token and its check status."""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

PENDING = "pending"
SENT = "sent"
FORWARDING = "forwarding"
BOUNCED = "bounced"
NOT_RECEIVED = "not_received"
STATUSES = (PENDING, SENT, FORWARDING, BOUNCED, NOT_RECEIVED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    email     TEXT PRIMARY KEY,
    token     TEXT UNIQUE NOT NULL,
    status    TEXT NOT NULL,
    sent_at   TEXT,
    found_at  TEXT,
    detail    TEXT NOT NULL DEFAULT ''
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


def new_token() -> str:
    return "GMC-" + secrets.token_hex(5).upper()


@dataclass
class Record:
    email: str
    token: str
    status: str
    sent_at: str | None
    found_at: str | None
    detail: str


class State:
    def __init__(self, path: str = "gmailcheck.db"):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "State":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- queueing ---------------------------------------------------------

    def add(self, emails: list[str]) -> int:
        """Queue addresses that aren't tracked yet. Returns how many were added."""
        added = 0
        with self.conn:
            for email in emails:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO checks (email, token, status) VALUES (?, ?, ?)",
                    (email, new_token(), PENDING),
                )
                added += cur.rowcount
        return added

    def requeue(self, statuses: tuple[str, ...]) -> int:
        """Put addresses back to pending with a fresh token so they get re-sent."""
        rows = self.conn.execute(
            f"SELECT email FROM checks WHERE status IN ({','.join('?' * len(statuses))})",
            statuses,
        ).fetchall()
        with self.conn:
            for row in rows:
                self.conn.execute(
                    "UPDATE checks SET status=?, token=?, sent_at=NULL, found_at=NULL, detail='' "
                    "WHERE email=?",
                    (PENDING, new_token(), row["email"]),
                )
        return len(rows)

    # -- queries ----------------------------------------------------------

    def _records(self, sql: str, params: tuple = ()) -> list[Record]:
        return [Record(**dict(r)) for r in self.conn.execute(sql, params).fetchall()]

    def pending(self, emails: list[str] | None = None) -> list[Record]:
        records = self._records("SELECT * FROM checks WHERE status=? ORDER BY rowid", (PENDING,))
        if emails is not None:
            wanted = set(emails)
            records = [r for r in records if r.email in wanted]
        return records

    def by_status(self, *statuses: str) -> list[Record]:
        return self._records(
            f"SELECT * FROM checks WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY rowid",
            statuses,
        )

    def all(self) -> list[Record]:
        return self._records("SELECT * FROM checks ORDER BY rowid")

    def token_map(self) -> dict[str, str]:
        """token -> email for every address that has been sent."""
        rows = self.conn.execute("SELECT token, email FROM checks WHERE sent_at IS NOT NULL")
        return {r["token"]: r["email"] for r in rows}

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in STATUSES}
        for row in self.conn.execute("SELECT status, COUNT(*) AS n FROM checks GROUP BY status"):
            counts[row["status"]] = row["n"]
        return counts

    def sent_in_last_24h(self) -> int:
        cutoff = iso(now() - timedelta(hours=24))
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM checks WHERE sent_at IS NOT NULL AND sent_at >= ?", (cutoff,)
        ).fetchone()
        return row["n"]

    def earliest_sent(self) -> datetime | None:
        row = self.conn.execute(
            "SELECT MIN(sent_at) AS m FROM checks WHERE sent_at IS NOT NULL"
        ).fetchone()
        return parse_iso(row["m"]) if row["m"] else None

    # -- transitions ------------------------------------------------------

    def mark_sent(self, email: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE checks SET status=?, sent_at=? WHERE email=?", (SENT, iso(now()), email)
            )

    def mark_forwarding(self, email: str, detail: str = "") -> bool:
        """Mark as forwarding. Overrides sent/not_received/bounced (arrival is proof)."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE checks SET status=?, found_at=?, detail=? "
                "WHERE email=? AND status != ?",
                (FORWARDING, iso(now()), detail, email, FORWARDING),
            )
        return cur.rowcount > 0

    def mark_bounced(self, email: str, detail: str = "", *, include_pending: bool = False) -> bool:
        allowed = (SENT, NOT_RECEIVED, PENDING) if include_pending else (SENT, NOT_RECEIVED)
        with self.conn:
            cur = self.conn.execute(
                f"UPDATE checks SET status=?, detail=?, sent_at=COALESCE(sent_at, ?) "
                f"WHERE email=? AND status IN ({','.join('?' * len(allowed))})",
                (BOUNCED, detail[:500], iso(now()), email, *allowed),
            )
        return cur.rowcount > 0

    def finalise(self, grace: timedelta) -> int:
        """Mark sent addresses older than `grace` with no arrival as not_received."""
        cutoff = iso(now() - grace)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE checks SET status=?, detail='no test email arrived in master mailbox' "
                "WHERE status=? AND sent_at <= ?",
                (NOT_RECEIVED, SENT, cutoff),
            )
        return cur.rowcount
