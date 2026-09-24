"""Check that SMTP and IMAP are reachable and the logins work, before sending anything."""

from __future__ import annotations

import imaplib
import smtplib
import socket
import sys
from dataclasses import replace

from . import config, imap, sender
from .config import ImapConfig, SmtpConfig

BLOCKED_HELP = (
    "Couldn't reach the mail server on any port. This network is blocking outgoing\n"
    "email ports (587/465). That's very common on VPS/cloud servers (AWS, Azure,\n"
    "Vultr, OVH...) and sometimes caused by a firewall or antivirus.\n"
    "Fix: run it from a home PC/laptop, or ask the provider to unblock ports 587/465."
)
AUTH_HELP = (
    "Use an app password, not your normal Google password: turn on 2-Step Verification,\n"
    "then create one at https://myaccount.google.com/apppasswords"
)


class PreflightError(Exception):
    pass


def _out(msg: str, end: str = "\n") -> None:
    print(msg, end=end, file=sys.stderr, flush=True)


def describe(exc: BaseException) -> str:
    if isinstance(exc, socket.gaierror):
        return "server name not found (check the host / internet connection)"
    if isinstance(exc, (TimeoutError, socket.timeout)) or "timed out" in str(exc).lower():
        return "connection timed out"
    if isinstance(exc, ConnectionRefusedError):
        return "connection refused"
    return str(exc) or type(exc).__name__


def check_smtp(cfg: SmtpConfig) -> SmtpConfig:
    """Log in over SMTP, falling back to port 465 (SSL). Returns the config that worked."""
    cfg.validate()
    candidates = [cfg]
    if cfg.port != 465:
        candidates.append(replace(cfg, port=465, security="ssl"))
    for cand in candidates:
        _out(f"  SMTP {cand.host}:{cand.port} ... ", end="")
        try:
            conn = sender._connect(cand)
        except smtplib.SMTPAuthenticationError as exc:
            _out("login rejected")
            raise PreflightError(f"Sender login for {cand.user} was rejected ({exc.smtp_code}).\n"
                                 + AUTH_HELP) from exc
        except (OSError, smtplib.SMTPException) as exc:
            _out(describe(exc))
            continue
        try:
            conn.quit()
        except Exception:
            pass
        _out(f"OK (logged in as {cand.user})")
        return cand
    raise PreflightError(BLOCKED_HELP)


def check_imap(cfg: ImapConfig, label: str) -> bool:
    _out(f"  IMAP {cfg.host}:{cfg.port} ({label}) ... ", end="")
    try:
        conn = imap.connect(cfg)
    except imap.ImapError as exc:
        _out("login rejected")
        raise PreflightError(f"{label} login for {cfg.user} was rejected: {exc}\n" + AUTH_HELP) from exc
    except (OSError, imaplib.IMAP4.error) as exc:
        _out(describe(exc))
        raise PreflightError(f"Couldn't reach {cfg.host}:{cfg.port} for the {label} mailbox "
                             f"({describe(exc)}).") from exc
    imap.logout(conn)
    _out(f"OK (logged in as {cfg.user})")
    return True


def run(smtp_preset: str | None = None) -> SmtpConfig:
    """Check everything. Returns the SMTP config to send with. Raises PreflightError."""
    _out("Checking connections...")
    smtp_cfg = check_smtp(config.smtp_config(smtp_preset))
    master = config.master_imap_config()
    master.validate("IMAP")
    check_imap(master, "master")
    bounce = config.bounce_imap_config()
    if bounce:
        try:
            check_imap(bounce, "sender, for bounces")
        except PreflightError as exc:
            _out(f"  (bounce detection off: {str(exc).splitlines()[0]})")
            config.disable_bounces()
    return smtp_cfg
