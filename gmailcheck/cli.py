"""Command-line interface."""

from __future__ import annotations

import argparse
import imaplib
import smtplib
import sys
import time
from datetime import timedelta
from pathlib import Path

from . import (__version__, bounces, checker, config, export, folder, imap,
               inputs, preflight)
from .config import SmtpConfig
from .sender import SmtpSender, build_message, throttle
from .state import (BOUNCED, FORWARDING, NOT_RECEIVED, PENDING, SENT, Record,
                    State, new_token)


def log(msg: str = "") -> None:
    print(msg, file=sys.stderr, flush=True)


class SendAborted(Exception):
    """Sending can't continue (connection lost, login rejected, provider limit)."""


# -- queue ------------------------------------------------------------------


def load_input(path: str) -> list[str]:
    result = inputs.load_emails(path)
    for lineno, raw in result.invalid:
        log(f"  skipping invalid address on line {lineno}: {raw!r}")
    if result.duplicates:
        log(f"  {result.duplicates} duplicate addresses dropped")
    return result.emails


def warn_same_account(smtp_user: str) -> None:
    master = config.master_imap_config().user
    if master and smtp_user and master.lower() == smtp_user.lower():
        log("WARNING: the sender is the same account as the master mailbox.\n"
            "         Gmail usually discards forwarded copies of mail you sent yourself,\n"
            "         so every address would look dead. Use a separate sender account.")


def dry_run(args: argparse.Namespace, st: State, emails: list[str] | None) -> int:
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


def build_queue(args: argparse.Namespace, st: State, emails: list[str] | None) -> list[Record]:
    """Track new addresses and return what may be sent now (within the daily cap)."""
    if emails is not None:
        st.add(emails)
    if args.resend_failed:
        log(f"Re-queued {st.requeue((NOT_RECEIVED,))} not_received addresses with fresh tokens.")

    counts = st.counts()
    done = counts[FORWARDING] + counts[BOUNCED] + counts[NOT_RECEIVED]
    log(f"Mail list loaded: {sum(counts.values())} addresses "
        f"({done} already done, {counts[SENT]} awaiting arrival, {counts[PENDING]} to send)")

    queue = st.pending()
    if not queue:
        return []
    budget = args.daily_cap - st.sent_in_last_24h()
    if budget <= 0:
        log(f"Daily cap of {args.daily_cap} reached for the last 24h; "
            f"{len(queue)} will be sent on a later run.")
        return []
    if args.limit:
        budget = min(budget, args.limit)
    if budget < len(queue):
        log(f"Sending allowance now: {budget} (daily cap {args.daily_cap}); "
            f"the other {len(queue) - budget} go on a later run.")
    return queue[:budget]


# -- sending ----------------------------------------------------------------


def send_one(smtp: SmtpSender, st: State, cfg: SmtpConfig, rec: Record) -> str:
    """Send one test email and record the outcome. Returns a short status for display."""
    try:
        smtp.send(build_message(cfg.sender, rec.email, rec.token))
    except smtplib.SMTPRecipientsRefused as exc:
        code, text = next(iter(exc.recipients.values()))
        text = text.decode(errors="replace") if isinstance(text, bytes) else str(text)
        st.mark_bounced(rec.email, f"SMTP refused {code}: {text}", include_pending=True)
        return f"refused ({code})"
    except smtplib.SMTPAuthenticationError as exc:
        raise SendAborted(f"sender login rejected ({exc.smtp_code}).\n" + preflight.AUTH_HELP) from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise SendAborted(f"{preflight.describe(exc)} while sending to {rec.email}") from exc
    st.mark_sent(rec.email)
    return "sent"


def line(n: int, total: int, email: str, status: str) -> str:
    width = len(str(total))
    return f"  [{n:>{width}}/{total}] {email} {'.' * max(2, 34 - len(email))} {status}"


def do_send(args: argparse.Namespace, st: State) -> int:
    log("Loading mails...")
    emails = load_input(args.input) if args.input else None
    if args.dry_run:
        return dry_run(args, st, emails)
    todo = build_queue(args, st, emails)
    if not todo:
        log("Nothing to send right now.")
        return 0

    smtp_cfg = config.smtp_config(args.smtp_preset)
    smtp_cfg.validate()
    warn_same_account(smtp_cfg.user)
    log(f"Sending {len(todo)} via {smtp_cfg.host}:{smtp_cfg.port} as {smtp_cfg.sender}")
    with SmtpSender(smtp_cfg) as smtp:
        for i, rec in enumerate(todo, 1):
            try:
                status = send_one(smtp, st, smtp_cfg, rec)
            except SendAborted as exc:
                log(f"Stopped: {exc}\nProgress is saved; run `send` again to continue.")
                return 2
            log(line(i, len(todo), rec.email, status))
            if i < len(todo):
                throttle(args.delay)
    log(f"Done. {len(st.pending())} still pending.")
    return 0


# -- checking ---------------------------------------------------------------


class InboxWatcher:
    """Keeps IMAP connections open across checks and reconnects when they drop."""

    def __init__(self, st: State, cleanup: bool = False, bounces_on: bool = True):
        self.st = st
        self.cleanup = cleanup
        self.bounces_on = bounces_on
        self._master: imaplib.IMAP4 | None = None
        self._bounce: imaplib.IMAP4 | None = None
        self._seen_bounces: set[bytes] = set()
        self.folders: list[str] = []

    def _master_conn(self) -> imaplib.IMAP4:
        if self._master is None:
            cfg = config.master_imap_config()
            cfg.validate("IMAP")
            self._master = imap.connect(cfg)
        return self._master

    def _bounce_conn(self) -> imaplib.IMAP4 | None:
        if self._bounce is None and self.bounces_on:
            cfg = config.bounce_imap_config()
            if cfg:
                self._bounce = imap.connect(cfg)
        return self._bounce

    def poll(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """Returns (newly forwarding, newly bounced) as lists of (email, detail)."""
        since = self.st.earliest_sent()
        if since is None:
            return [], []
        for attempt in range(2):
            try:
                return self._poll(since)
            except (imaplib.IMAP4.abort, OSError) as exc:
                self.close()
                if attempt:
                    log(f"  (inbox check failed: {preflight.describe(exc)}; will retry later)")
        return [], []

    def _poll(self, since) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        tokens = self.st.token_map()
        conn = self._master_conn()
        result = checker.scan_master(conn, since, set(tokens))
        self.folders = result.folders_scanned
        arrived = []
        for token, label in result.found.items():
            detail = "landed in spam" if label == "spam" else ""
            if self.st.mark_forwarding(tokens[token], detail):
                arrived.append((tokens[token], detail))
        if self.cleanup and result.uids:
            checker.cleanup(conn, result)

        bounced = []
        bconn = self._bounce_conn()
        if bconn is not None:
            sent_emails = set(tokens.values())
            for b in bounces.scan_bounces(bconn, since, self._seen_bounces):
                targets = {tokens[t] for t in b.tokens if t in tokens} or (b.recipients & sent_emails)
                for addr in targets:
                    if self.st.mark_bounced(addr, b.reason):
                        bounced.append((addr, b.reason))
        return arrived, bounced

    def close(self) -> None:
        for conn in (self._master, self._bounce):
            if conn is not None:
                imap.logout(conn)
        self._master = self._bounce = None

    def __enter__(self) -> "InboxWatcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def report_poll(arrived: list[tuple[str, str]], bounced: list[tuple[str, str]]) -> None:
    for email, detail in arrived:
        log(f"  {email} {'.' * max(2, 34 - len(email))} FORWARDING" + (f" ({detail})" if detail else ""))
    for email, reason in bounced:
        log(f"  {email} {'.' * max(2, 34 - len(email))} BOUNCED ({reason[:80]})")


def totals(st: State) -> str:
    c = st.counts()
    return (f"Totals: {c[FORWARDING]} forwarding, {c[BOUNCED]} bounced, "
            f"{c[NOT_RECEIVED]} not received, {c[SENT]} waiting to arrive, {c[PENDING]} not sent yet")


def do_check(args: argparse.Namespace, st: State) -> int:
    if st.earliest_sent() is None:
        log("Nothing has been sent yet.")
        return 0
    with InboxWatcher(st, args.cleanup, not args.no_bounces) as watcher:
        arrived, bounced = watcher.poll()
    report_poll(arrived, bounced)
    msg = f"Scanned {', '.join(watcher.folders) or 'nothing'}: {len(arrived)} newly forwarding, {len(bounced)} newly bounced"
    if args.final:
        msg += f", {st.finalise(timedelta(minutes=args.grace))} marked not_received"
    log(msg + ".")
    print_status(st)
    return 0


# -- status / export --------------------------------------------------------


def print_status(st: State) -> None:
    log(totals(st))
    if st.counts()[SENT]:
        log("  Some are still waiting to arrive; run `check` again later "
            "or `check --final` to mark them not_received.")


def do_status(args: argparse.Namespace, st: State) -> int:
    print_status(st)
    return 0


def write_outputs(args: argparse.Namespace, st: State, quiet: bool = False) -> None:
    ok = st.by_status(FORWARDING)
    export.write(ok, args.output, args.format)
    if getattr(args, "output_txt", None):
        export.write(ok, args.output_txt, "txt")
    failed = st.by_status(BOUNCED, NOT_RECEIVED)
    if args.failed:
        export.write(failed, args.failed, args.format)
    waiting = st.by_status(SENT, PENDING)
    if getattr(args, "pending", None):
        export.write(waiting, args.pending, args.format)
    if not quiet:
        log(f"Wrote {len(ok)} forwarding addresses to {args.output}")
        if args.failed:
            log(f"Wrote {len(failed)} not forwarding addresses to {args.failed}")
        if getattr(args, "pending", None):
            log(f"Wrote {len(waiting)} unfinished addresses to {args.pending}")
        if waiting:
            log(f"Note: {len(waiting)} addresses are not finished yet "
                "(not sent, or sent but not arrived); run again later.")


def do_export(args: argparse.Namespace, st: State) -> int:
    write_outputs(args, st)
    return 0


# -- run (batched) ----------------------------------------------------------


def do_run(args: argparse.Namespace, st: State) -> int:
    log("Loading mails...")
    emails = load_input(args.input)
    if args.dry_run:
        return dry_run(args, st, emails)
    if not emails and not st.all():
        log("No addresses found.")
        return 1

    try:
        smtp_cfg = preflight.run(args.smtp_preset)
    except preflight.PreflightError as exc:
        log(f"\nCan't start: {exc}")
        return 2
    warn_same_account(smtp_cfg.user)
    log("")

    todo = build_queue(args, st, emails)
    size = max(1, args.batch_size)
    batches = [todo[i:i + size] for i in range(0, len(todo), size)]

    with InboxWatcher(st, args.cleanup, not args.no_bounces) as watcher:
        if batches:
            with SmtpSender(smtp_cfg) as smtp:
                n = 0
                for b, batch in enumerate(batches, 1):
                    log(f"\nBatch {b} of {len(batches)} starting ({len(batch)} mails)")
                    sent = 0
                    for i, rec in enumerate(batch):
                        n += 1
                        try:
                            status = send_one(smtp, st, smtp_cfg, rec)
                        except SendAborted as exc:
                            log(f"\nStopped: {exc}\nProgress is saved; run it again to continue.")
                            write_outputs(args, st, quiet=True)
                            return 2
                        sent += status == "sent"
                        log(line(n, len(todo), rec.email, status))
                        if i < len(batch) - 1:
                            throttle(args.delay)
                    log(f"Batch {b} done: {sent} sent")
                    log("Checking master inbox...")
                    report_poll(*watcher.poll())
                    log(totals(st))
                    write_outputs(args, st, quiet=True)
                    if b < len(batches):
                        log(f"Waiting {args.batch_pause:g}s before next batch...")
                        time.sleep(args.batch_pause)

        # Give the last emails time to arrive.
        deadline = time.monotonic() + args.wait
        while st.counts()[SENT] and time.monotonic() < deadline:
            left = int(deadline - time.monotonic())
            log(f"\n{st.counts()[SENT]} still waiting to arrive; checking again in "
                f"{args.interval}s (giving up in {left // 60}m {left % 60}s)")
            time.sleep(min(args.interval, max(deadline - time.monotonic(), 0)))
            report_poll(*watcher.poll())

    gave_up = st.finalise(timedelta(seconds=args.wait))
    log("")
    if gave_up:
        log(f"{gave_up} never arrived: marked not received.")
    log(totals(st))
    write_outputs(args, st)
    return 0


def do_test(args: argparse.Namespace, st: State | None = None) -> int:
    try:
        preflight.run(getattr(args, "smtp_preset", None))
    except preflight.PreflightError as exc:
        log(f"\nProblem: {exc}")
        return 2
    log("\nAll good: ready to send.")
    return 0


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

    def send_opts(sp: argparse.ArgumentParser, input_required: bool | None) -> None:
        if input_required is not None:
            sp.add_argument("input", nargs=None if input_required else "?",
                            help="addresses as .txt (one per line) or .csv, or a folder of them")
        sp.add_argument("--smtp-preset", choices=sorted(config.SMTP_PRESETS),
                        help="fill SMTP host/port/security (overrides SMTP_PRESET)")
        sp.add_argument("--delay", type=float, default=2.0,
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
        sp.add_argument("--failed", help="also write bounced / not received addresses here")
        sp.add_argument("--pending", help="also write unfinished (unsent / not arrived yet) here")
        sp.add_argument("--format", choices=("csv", "txt"),
                        help="output format (default: from file extension)")

    def run_opts(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--batch-size", type=int, default=25,
                        help="emails per batch (default: %(default)s)")
        sp.add_argument("--batch-pause", type=float, default=15,
                        help="seconds to pause between batches (default: %(default)s)")
        sp.add_argument("--wait", type=int, default=600,
                        help="seconds to keep checking after the last batch (default: %(default)s)")
        sp.add_argument("--interval", type=int, default=30,
                        help="seconds between checks while waiting (default: %(default)s)")

    sp = sub.add_parser("send", help="queue addresses and send test emails (no batching)")
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

    sp = sub.add_parser("run", help="send in batches, checking the inbox between them, then export")
    send_opts(sp, input_required=True)
    check_opts(sp)
    export_opts(sp)
    run_opts(sp)
    sp.set_defaults(func=do_run)

    sp = sub.add_parser("folder", help="run using the emails/, success/ and failed/ folders "
                                       "and accounts.csv (what a double-click does)")
    sp.add_argument("--dir", help="the working folder (default: next to the program)")
    send_opts(sp, input_required=None)
    check_opts(sp)
    run_opts(sp)
    sp.set_defaults(func=do_run, format=None)

    sp = sub.add_parser("test", help="only check the connections and logins")
    sp.add_argument("--dir", help="folder with accounts.csv (default: next to the program)")
    sp.add_argument("--smtp-preset", choices=sorted(config.SMTP_PRESETS))
    sp.set_defaults(func=do_test)
    return p


def folder_base(args: argparse.Namespace) -> Path:
    return Path(args.dir).resolve() if getattr(args, "dir", None) else folder.app_dir()


def prepare_folder(args: argparse.Namespace) -> bool:
    """Point the run at the folder layout. Returns False if setup is incomplete."""
    base = folder_base(args)
    log(f"Working folder: {base}")
    problems = folder.prepare(base)
    if problems:
        for p in problems:
            log(f"  - {p}")
        log("Then run it again.")
        return False
    folder.apply_accounts(folder.load_accounts(base / folder.ACCOUNTS_FILE))
    args.db = str(base / "data" / "gmailcheck.db")
    args.input = str(base / "emails")
    args.output = str(base / "success" / "forwarding.csv")
    args.output_txt = str(base / "success" / "forwarding.txt")
    args.failed = str(base / "failed" / "not_forwarding.csv")
    args.pending = str(base / "failed" / "pending.csv")
    return True


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    # Double-clicked (no arguments): run in folder mode and keep the window open.
    pause = not argv
    args = build_parser().parse_args(argv or ["folder"])
    try:
        return _main(args)
    finally:
        if pause:
            try:
                input("\nPress Enter to close...")
            except (EOFError, KeyboardInterrupt):
                pass


def _main(args: argparse.Namespace) -> int:
    try:
        if args.command == "folder":
            if not prepare_folder(args):
                return 1
        elif args.command == "test":
            accounts = folder_base(args) / folder.ACCOUNTS_FILE
            if accounts.exists():
                folder.apply_accounts(folder.load_accounts(accounts))
            else:
                config.load_dotenv(args.env)
            return do_test(args)
        else:
            config.load_dotenv(args.env)
        with State(args.db) as st:
            return args.func(args, st)
    except (config.ConfigError, imap.ImapError) as exc:
        log(f"error: {exc}")
        return 2
    except KeyboardInterrupt:
        log("\nInterrupted; progress is saved.")
        return 130
    except Exception as exc:  # keep the message visible when double-clicked
        log(f"unexpected error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
