"""Command-line interface."""

from __future__ import annotations

import argparse
import smtplib
import sys
import time
from datetime import timedelta

from . import __version__, bounces, checker, config, export, imap, inputs
from .sender import SmtpSender, build_message, throttle
from .state import (BOUNCED, FORWARDING, NOT_RECEIVED, PENDING, SENT, State,
                    new_token)


def log(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


# -- send -------------------------------------------------------------------


def load_input(path: str) -> list[str]:
    result = inputs.load_emails(path)
    for lineno, raw in result.invalid:
        log(f"  skipping invalid address on line {lineno}: {raw!r}")
    extra = f", {result.duplicates} duplicates dropped" if result.duplicates else ""
    log(f"Loaded {len(result.emails)} addresses from {path}{extra}")
    return result.emails


def warn_same_account(smtp_user: str) -> None:
    master = config.master_imap_config().user
    if master and smtp_user and master.lower() == smtp_user.lower():
        log("WARNING: SMTP_USER is the same account as the master mailbox (IMAP_USER).\n"
            "         Gmail usually discards forwarded copies of mail you sent yourself,\n"
            "         so every address would look dead. Use a separate sender account.")


def do_send(args: argparse.Namespace, st: State) -> int:
    emails = load_input(args.input) if args.input else None

    if args.dry_run:
        known = {r.email: r for r in st.all()}
        new = [e for e in (emails or []) if e not in known]
        todo = list(dict.fromkeys(new + [r.email for r in st.pending()]))
        cap = min(args.daily_cap - st.sent_in_last_24h(), args.limit or len(todo))
        log(f"Dry run: {len(todo)} to send, {max(cap, 0)} would go out now "
            f"(daily cap {args.daily_cap}).")
        if todo:
            sample = build_message(config.smtp_config(args.smtp_preset).sender or "sender@example.com",
                                   todo[0], known[todo[0]].token if todo[0] in known else new_token())
            log("\n--- sample message ---\n" + sample.as_string())
        return 0

    if emails is not None:
        added = st.add(emails)
        log(f"Queued {added} new addresses ({len(emails) - added} already tracked).")
    if args.resend_failed:
        log(f"Re-queued {st.requeue((NOT_RECEIVED,))} not_received addresses with fresh tokens.")

    queue = st.pending()
    if not queue:
        log("Nothing pending to send.")
        return 0

    budget = args.daily_cap - st.sent_in_last_24h()
    if budget <= 0:
        log(f"Daily cap of {args.daily_cap} reached (last 24h). Run `send` again later.")
        return 0
    if args.limit:
        budget = min(budget, args.limit)
    batch = queue[:budget]

    smtp_cfg = config.smtp_config(args.smtp_preset)
    smtp_cfg.validate()
    warn_same_account(smtp_cfg.user)
    log(f"Sending {len(batch)} of {len(queue)} pending via {smtp_cfg.host}:{smtp_cfg.port} "
        f"as {smtp_cfg.sender}")

    sent = refused = 0
    with SmtpSender(smtp_cfg) as smtp:
        for i, rec in enumerate(batch, 1):
            msg = build_message(smtp_cfg.sender, rec.email, rec.token)
            try:
                smtp.send(msg)
            except smtplib.SMTPRecipientsRefused as exc:
                code, text = next(iter(exc.recipients.values()))
                detail = f"SMTP refused {code}: {text.decode(errors='replace') if isinstance(text, bytes) else text}"
                st.mark_bounced(rec.email, detail, include_pending=True)
                refused += 1
                log(f"  [{i}/{len(batch)}] {rec.email}: refused ({code})")
                continue
            except smtplib.SMTPAuthenticationError as exc:
                log(f"SMTP login failed: {exc}. For Gmail use an app password "
                    "(Google Account > Security > App passwords).")
                return 2
            except (smtplib.SMTPException, OSError) as exc:
                log(f"Stopping: SMTP error on {rec.email}: {exc}\n"
                    "Progress is saved; run `send` again later to continue.")
                break
            st.mark_sent(rec.email)
            sent += 1
            log(f"  [{i}/{len(batch)}] sent {rec.email} ({rec.token})")
            if i < len(batch):
                throttle(args.delay)

    remaining = len(st.pending())
    log(f"Sent {sent}, refused {refused}. {remaining} still pending"
        + (" (run `send` again after the daily window)." if remaining else "."))
    return 0


# -- check ------------------------------------------------------------------


def do_check(args: argparse.Namespace, st: State, quiet: bool = False) -> int:
    since = st.earliest_sent()
    if since is None:
        log("Nothing has been sent yet.")
        return 0
    tokens = st.token_map()

    cfg = config.master_imap_config()
    cfg.validate("IMAP")
    conn = imap.connect(cfg)
    try:
        result = checker.scan_master(conn, since, set(tokens))
        newly = 0
        for token, label in result.found.items():
            detail = "landed in spam" if label == "spam" else ""
            if st.mark_forwarding(tokens[token], detail):
                newly += 1
        if args.cleanup and result.uids:
            log(f"Moved {checker.cleanup(conn, result)} test emails to Trash.")
    finally:
        imap.logout(conn)

    bounced = 0
    bcfg = config.bounce_imap_config()
    if bcfg and not args.no_bounces:
        bconn = imap.connect(bcfg)
        try:
            sent_emails = set(tokens.values())
            for b in bounces.scan_bounces(bconn, since):
                targets = {tokens[t] for t in b.tokens if t in tokens}
                if not targets:
                    targets = b.recipients & sent_emails
                for addr in targets:
                    if st.mark_bounced(addr, b.reason):
                        bounced += 1
        finally:
            imap.logout(bconn)

    finalised = st.finalise(timedelta(minutes=args.grace)) if args.final else 0

    if not quiet:
        log(f"Scanned {', '.join(result.folders_scanned) or 'nothing'}: "
            f"{newly} newly confirmed forwarding, {bounced} newly bounced"
            + (f", {finalised} marked not_received." if args.final else "."))
        print_status(st)
    return 0


# -- status / export --------------------------------------------------------


def print_status(st: State) -> None:
    counts = st.counts()
    total = sum(counts.values())
    log(f"Total {total}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    if counts[SENT]:
        log(f"  {counts[SENT]} sent but not seen yet; run `check` again later "
            "or `check --final` to mark them not_received.")


def do_status(args: argparse.Namespace, st: State) -> int:
    print_status(st)
    return 0


def do_export(args: argparse.Namespace, st: State) -> int:
    ok = export.write(st.by_status(FORWARDING), args.output, args.format)
    log(f"Wrote {ok} forwarding addresses to {args.output}")
    if args.failed:
        failed = st.by_status(BOUNCED, NOT_RECEIVED, SENT, PENDING)
        export.write(failed, args.failed, args.format)
        log(f"Wrote {len(failed)} other addresses to {args.failed}")
    counts = st.counts()
    if counts[SENT] or counts[PENDING]:
        log(f"Note: {counts[SENT]} sent and {counts[PENDING]} pending are not final yet.")
    return 0


# -- run --------------------------------------------------------------------


def do_run(args: argparse.Namespace, st: State) -> int:
    rc = do_send(args, st)
    if rc or args.dry_run:
        return rc
    deadline = time.monotonic() + args.wait
    args.final = False
    while True:
        do_check(args, st, quiet=True)
        waiting = st.counts()[SENT]
        if not waiting or time.monotonic() >= deadline:
            break
        log(f"  {waiting} still in flight; checking again in {args.interval}s...")
        time.sleep(min(args.interval, max(deadline - time.monotonic(), 1)))
    args.final = True
    args.grace = args.wait / 60
    do_check(args, st)
    return do_export(args, st)


# -- parser -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gmailcheck",
        description="Check which Gmail addresses still forward to your master mailbox.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--db", default="gmailcheck.db", help="state database (default: %(default)s)")
    p.add_argument("--env", default=".env", help="env file to load (default: %(default)s)")
    sub = p.add_subparsers(dest="command", required=True)

    def send_opts(sp: argparse.ArgumentParser, input_required: bool) -> None:
        sp.add_argument("input", nargs=None if input_required else "?",
                        help="addresses as .txt (one per line) or .csv")
        sp.add_argument("--smtp-preset", choices=sorted(config.SMTP_PRESETS),
                        help="fill SMTP host/port/security (overrides SMTP_PRESET)")
        sp.add_argument("--delay", type=float, default=3.0,
                        help="seconds between sends, plus jitter (default: %(default)s)")
        sp.add_argument("--daily-cap", type=int, default=400,
                        help="max sends per rolling 24h (default: %(default)s)")
        sp.add_argument("--limit", type=int, help="send at most N in this run")
        sp.add_argument("--resend-failed", action="store_true",
                        help="re-queue not_received addresses with new tokens")
        sp.add_argument("--dry-run", action="store_true", help="show what would be sent")

    def check_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--cleanup", action="store_true",
                        help="move found test emails in the master mailbox to Trash")
        sp.add_argument("--no-bounces", action="store_true", help="skip bounce scanning")

    def export_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("-o", "--output", default="success.csv",
                        help="file for forwarding addresses (default: %(default)s)")
        sp.add_argument("--failed", help="also write non-forwarding addresses here")
        sp.add_argument("--format", choices=("csv", "txt"),
                        help="output format (default: from file extension)")

    sp = sub.add_parser("send", help="queue addresses and send test emails")
    send_opts(sp, input_required=False)
    sp.set_defaults(func=do_send)

    sp = sub.add_parser("check", help="look for arrived test emails and bounces")
    check_opts(sp)
    sp.add_argument("--final", action="store_true",
                    help="mark sent addresses older than --grace as not_received")
    sp.add_argument("--grace", type=float, default=30,
                    help="minutes to wait before --final gives up (default: %(default)s)")
    sp.set_defaults(func=do_check)

    sp = sub.add_parser("status", help="show counts per status")
    sp.set_defaults(func=do_status)

    sp = sub.add_parser("export", help="write results to CSV or TXT")
    export_opts(sp)
    sp.set_defaults(func=do_export)

    sp = sub.add_parser("run", help="send, wait for arrivals, then export")
    send_opts(sp, input_required=True)
    check_opts(sp)
    export_opts(sp)
    sp.add_argument("--wait", type=int, default=900,
                    help="seconds to wait for forwarded mail after sending (default: %(default)s)")
    sp.add_argument("--interval", type=int, default=60,
                    help="seconds between checks while waiting (default: %(default)s)")
    sp.set_defaults(func=do_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config.load_dotenv(args.env)
    try:
        with State(args.db) as st:
            return args.func(args, st)
    except (config.ConfigError, imap.ImapError) as exc:
        log(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        log("\nInterrupted; progress is saved in the state database.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
