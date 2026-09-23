"""Load and normalise email address lists from .txt or .csv files."""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


@dataclass
class LoadResult:
    emails: list[str] = field(default_factory=list)
    invalid: list[tuple[int, str]] = field(default_factory=list)  # (line number, raw value)
    duplicates: int = 0


def normalise(raw: str) -> str:
    return raw.strip().strip('"').strip("'").strip().lower()


def _collect(values: list[tuple[int, str]]) -> LoadResult:
    result = LoadResult()
    seen: set[str] = set()
    for lineno, raw in values:
        email = normalise(raw)
        if not email:
            continue
        if not EMAIL_RE.match(email):
            result.invalid.append((lineno, raw))
            continue
        if email in seen:
            result.duplicates += 1
            continue
        seen.add(email)
        result.emails.append(email)
    return result


def _read_txt(path: Path) -> list[tuple[int, str]]:
    values = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if line and not line.startswith("#"):
            values.append((lineno, line))
    return values


def _read_csv(path: Path) -> list[tuple[int, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return []

    header = [c.strip().lower() for c in rows[0]]
    col = None
    start = 0
    for name in ("email", "e-mail", "email address", "mail", "address"):
        if name in header:
            col, start = header.index(name), 1
            break

    if col is None:
        # No recognised header: use the first column that contains an "@" in any row.
        for row in rows:
            for i, cell in enumerate(row):
                if "@" in cell:
                    col = i
                    break
            if col is not None:
                break
        if col is None:
            return []
        # Skip a header row that has no "@" in the chosen column.
        if "@" not in (rows[0][col] if col < len(rows[0]) else ""):
            start = 1

    values = []
    for lineno, row in enumerate(rows[start:], start + 1):
        if col < len(row) and row[col].strip():
            values.append((lineno, row[col]))
    return values


def load_emails(path: str | Path) -> LoadResult:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return _collect(_read_csv(path))
    return _collect(_read_txt(path))
