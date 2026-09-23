"""Write results to CSV or plain text."""

from __future__ import annotations

import csv
from pathlib import Path

from .state import Record

CSV_FIELDS = ("email", "status", "sent_at", "found_at", "detail")


def detect_format(path: str | Path, fmt: str | None) -> str:
    if fmt:
        return fmt
    return "txt" if Path(path).suffix.lower() == ".txt" else "csv"


def write(records: list[Record], path: str | Path, fmt: str | None = None) -> int:
    fmt = detect_format(path, fmt)
    path = Path(path)
    if fmt == "txt":
        path.write_text("".join(f"{r.email}\n" for r in records), encoding="utf-8")
    else:
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_FIELDS)
            for r in records:
                writer.writerow([r.email, r.status, r.sent_at or "", r.found_at or "", r.detail])
    return len(records)
