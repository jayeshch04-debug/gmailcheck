"""Folder mode: everything lives next to the program, so it can be run by double-click.

    GmailCheck/
      gmailcheck.exe
      accounts.csv        master + sender logins (app passwords)
      emails/             one or more .txt / .csv lists of addresses to check
      success/            forwarding.csv + forwarding.txt (written by the program)
      failed/             not_forwarding.csv (written by the program)
      data/               progress database, so runs can continue another day
"""

from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

from .config import ConfigError
from .inputs import load_emails

ACCOUNTS_FILE = "accounts.csv"
ACCOUNTS_FIELDS = ("role", "email", "app_password", "smtp_host", "smtp_port", "imap_host")
ACCOUNTS_TEMPLATE = (
    "role,email,app_password,smtp_host,smtp_port,imap_host\n"
    "master,your.master@gmail.com,abcd efgh ijkl mnop,,,\n"
    "sender,a.different.account@gmail.com,abcd efgh ijkl mnop,,,\n"
)


def app_dir() -> Path:
    """Folder holding the .exe when frozen, otherwise the current directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def prepare(base: Path) -> list[str]:
    """Create the folder layout. Returns what still needs the user's attention."""
    for name in ("emails", "success", "failed", "data"):
        (base / name).mkdir(exist_ok=True)
    problems = []
    accounts = base / ACCOUNTS_FILE
    if not accounts.exists():
        accounts.write_text(ACCOUNTS_TEMPLATE, encoding="utf-8")
        problems.append(f"Created {accounts.name}: fill in your master and sender accounts.")
    elif "your.master@gmail.com" in accounts.read_text(encoding="utf-8-sig"):
        problems.append(f"{accounts.name} still has the example addresses: fill in your accounts.")
    if not load_emails(base / "emails").emails:
        problems.append("Put your list of addresses (.txt or .csv) in the 'emails' folder.")
    return problems


def load_accounts(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "role" not in [f.strip().lower() for f in reader.fieldnames]:
            raise ConfigError(f"{path.name} needs a header row: {','.join(ACCOUNTS_FIELDS)}")
        accounts = {}
        for row in reader:
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            role = row.get("role", "").lower()
            if role:
                accounts[role] = row
    return accounts


def apply_accounts(accounts: dict[str, dict[str, str]]) -> None:
    """Turn accounts.csv rows into the environment variables the rest of the app reads."""
    master = accounts.get("master")
    sender = accounts.get("sender")
    if not master or not master.get("email") or not master.get("app_password"):
        raise ConfigError("accounts.csv needs a 'master' row with email and app_password")
    if not sender or not sender.get("email") or not sender.get("app_password"):
        raise ConfigError(
            "accounts.csv needs a 'sender' row with email and app_password. It must be a "
            "different account from master: Gmail drops forwarded copies of your own mail.")

    env = {
        "IMAP_USER": master["email"],
        "IMAP_PASS": master["app_password"],
        "IMAP_HOST": master.get("imap_host") or "imap.gmail.com",
        "SMTP_USER": sender["email"],
        "SMTP_PASS": sender["app_password"],
        "SMTP_FROM": sender["email"],
    }
    if sender.get("smtp_host"):
        port = sender.get("smtp_port") or "587"
        env.update(SMTP_PRESET="", SMTP_HOST=sender["smtp_host"], SMTP_PORT=port,
                   SMTP_SECURITY="ssl" if port == "465" else "starttls")
        bounce_host = sender.get("imap_host")
    else:
        env.update(SMTP_PRESET="gmail", SMTP_HOST="", SMTP_PORT="", SMTP_SECURITY="")
        bounce_host = sender.get("imap_host") or "imap.gmail.com"

    if bounce_host:
        env.update(BOUNCE_IMAP_USER=sender["email"], BOUNCE_IMAP_PASS=sender["app_password"],
                   BOUNCE_IMAP_HOST=bounce_host)
    else:
        env.update(BOUNCE_IMAP_USER="", BOUNCE_IMAP_PASS="")

    for key, value in env.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
