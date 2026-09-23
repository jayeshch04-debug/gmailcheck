"""In-memory stand-ins for smtplib.SMTP and imaplib.IMAP4."""

from __future__ import annotations

import smtplib


class FakeSMTP:
    def __init__(self, refuse: set[str] | None = None):
        self.sent = []
        self.refuse = refuse or set()

    def send_message(self, msg):
        to = msg["To"]
        if to in self.refuse:
            raise smtplib.SMTPRecipientsRefused({to: (550, b"5.1.1 user unknown")})
        self.sent.append(msg)

    def quit(self):
        pass

    def close(self):
        pass


class FakeIMAP:
    """Mailboxes keyed by name, each a list of raw messages (UIDs are 1-based)."""

    def __init__(self, mailboxes: dict[str, list[bytes]], roles: dict[str, str] | None = None):
        self.mailboxes = {k: list(v) for k, v in mailboxes.items()}
        self.roles = roles or {}
        self.selected = None
        self.moved: list[tuple[str, str, str]] = []

    def list(self):
        lines = []
        for name in self.mailboxes:
            flags = " ".join(["\\HasNoChildren"] + [r for r, n in self.roles.items() if n == name])
            lines.append(f'({flags}) "/" "{name}"'.encode())
        return "OK", lines

    def select(self, mailbox, readonly=True):
        name = mailbox.strip('"')
        if name not in self.mailboxes:
            return "NO", [b"no such mailbox"]
        self.selected = name
        return "OK", [str(len(self.mailboxes[name])).encode()]

    def uid(self, command, *args):
        msgs = self.mailboxes[self.selected]
        if command == "SEARCH":
            return "OK", [" ".join(str(i + 1) for i in range(len(msgs))).encode()]
        if command == "FETCH":
            data = []
            for uid in args[0].split(","):
                raw = msgs[int(uid) - 1]
                data.append((f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw))
                data.append(b")")
            return "OK", data
        if command == "MOVE":
            self.moved.append((self.selected, args[0], args[1]))
            return "OK", [None]
        return "NO", [None]

    def expunge(self):
        return "OK", [None]

    def logout(self):
        return "BYE", [None]


def forwarded(token: str, to: str) -> bytes:
    return (f"From: sender@example.com\r\nTo: {to}\r\n"
            f"Subject: Forward check {token}\r\nX-GmailCheck-Token: {token}\r\n\r\nbody\r\n").encode()


def gmail_bounce(token: str, to: str) -> bytes:
    return (
        "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\r\n"
        f"X-Failed-Recipients: {to}\r\n"
        "Subject: Delivery Status Notification (Failure)\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b1"\r\n\r\n'
        "--b1\r\nContent-Type: text/plain\r\n\r\nAddress not found\r\n"
        "--b1\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; googlemail.com\r\n\r\n"
        f"Final-Recipient: rfc822; {to}\r\nAction: failed\r\nStatus: 5.1.1\r\n"
        "Diagnostic-Code: smtp; 550-5.1.1 The email account that you tried to reach does not exist.\r\n"
        "--b1\r\nContent-Type: text/rfc822-headers\r\n\r\n"
        f"To: {to}\r\nSubject: Forward check {token}\r\nX-GmailCheck-Token: {token}\r\n"
        "--b1--\r\n"
    ).encode()


def delay_notice(to: str) -> bytes:
    return (
        "From: mailer-daemon@googlemail.com\r\n"
        "Subject: Delivery Status Notification (Delay)\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b1"\r\n\r\n'
        "--b1\r\nContent-Type: message/delivery-status\r\n\r\n"
        f"Final-Recipient: rfc822; {to}\r\nAction: delayed\r\nStatus: 4.4.1\r\n"
        "--b1--\r\n"
    ).encode()
