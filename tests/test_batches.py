"""Batched run, connection pre-check and tolerant accounts.csv."""

import os
import socket

import pytest

from gmailcheck import cli, folder, imap, preflight, sender
from gmailcheck.state import State

from .fakes import FakeIMAP, FakeSMTP, forwarded

ENV = {"SMTP_HOST": "smtp.gmail.com", "SMTP_PORT": "587", "SMTP_USER": "checker@gmail.com",
       "SMTP_PASS": "x", "IMAP_USER": "master@gmail.com", "IMAP_PASS": "y"}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k in ("SMTP_PRESET", "SMTP_SECURITY", "BOUNCE_IMAP_USER", "BOUNCE_IMAP_PASS", "IMAP_HOST"):
        monkeypatch.delenv(k, raising=False)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(sender.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return tmp_path


class LiveMaster:
    """A master mailbox where every sent email 'arrives' one poll later."""

    def __init__(self, smtp, skip=()):
        self.smtp, self.skip, self.polls, self.delivered = smtp, set(skip), 0, []

    def connect(self, cfg):
        return self

    def list(self):
        self.polls += 1
        # Deliver what was sent before this poll (so arrivals lag one check behind).
        self.delivered = [forwarded(m["X-GmailCheck-Token"], m["To"]) for m in self.smtp.sent
                          if m["To"] not in self.skip]
        return FakeIMAP({"[Gmail]/All Mail": []}, roles={"\\All": "[Gmail]/All Mail"}).list()

    def select(self, mailbox, readonly=True):
        return "OK", [b"1"]

    def uid(self, command, *args):
        box = FakeIMAP({"[Gmail]/All Mail": self.delivered})
        box.selected = "[Gmail]/All Mail"
        return box.uid(command, *args)

    def logout(self):
        pass


def test_batched_run(env, monkeypatch, capsys):
    (env / "list.txt").write_text("".join(f"user{i}@gmail.com\n" for i in range(60)))
    smtp = FakeSMTP()
    monkeypatch.setattr(sender, "_connect", lambda cfg: smtp)
    master = LiveMaster(smtp, skip={"user7@gmail.com"})
    monkeypatch.setattr(imap, "connect", master.connect)

    rc = cli.main(["run", "list.txt", "--batch-size", "25", "--wait", "0",
                   "-o", "ok.txt", "--failed", "bad.csv"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Mail list loaded: 60 addresses" in err
    assert "Batch 1 of 3 starting (25 mails)" in err
    assert "Batch 3 of 3 starting (10 mails)" in err
    assert "user0@gmail.com" in err and "FORWARDING" in err
    assert len(smtp.sent) == 60
    ok = (env / "ok.txt").read_text().splitlines()
    assert len(ok) == 59 and "user7@gmail.com" not in ok
    assert "user7@gmail.com,not_received" in (env / "bad.csv").read_text()


def test_outputs_written_after_each_batch_and_pending_separate(env, monkeypatch):
    (env / "list.txt").write_text("".join(f"u{i}@gmail.com\n" for i in range(30)))
    smtp = FakeSMTP()
    monkeypatch.setattr(sender, "_connect", lambda cfg: smtp)
    monkeypatch.setattr(imap, "connect", LiveMaster(smtp).connect)
    rc = cli.main(["run", "list.txt", "--daily-cap", "10", "--batch-size", "5", "--wait", "0",
                   "-o", "ok.csv", "--failed", "bad.csv", "--pending", "todo.csv"])
    assert rc == 0
    assert len(smtp.sent) == 10
    assert (env / "bad.csv").read_text().splitlines() == ["email,status,sent_at,found_at,detail"]
    assert len((env / "todo.csv").read_text().splitlines()) == 1 + 20


def test_preflight_falls_back_to_465(env, monkeypatch, capsys):
    smtp = FakeSMTP()
    used = []

    def connect(cfg):
        used.append((cfg.port, cfg.security))
        if cfg.port == 587:
            raise TimeoutError("[WinError 10060] timed out")
        return smtp
    monkeypatch.setattr(sender, "_connect", connect)
    monkeypatch.setattr(imap, "connect", lambda cfg: FakeIMAP({"INBOX": []}))
    (env / "list.txt").write_text("a@gmail.com\n")
    assert cli.main(["run", "list.txt", "--wait", "0"]) == 0
    err = capsys.readouterr().err
    assert "587 ... connection timed out" in err
    assert "465 ... OK" in err
    assert used[-1] == (465, "ssl") and len(smtp.sent) == 1


def test_blocked_ports_stop_before_sending(env, monkeypatch, capsys):
    def connect(cfg):
        raise socket.timeout("timed out")
    monkeypatch.setattr(sender, "_connect", connect)
    (env / "list.txt").write_text("a@gmail.com\n")
    assert cli.main(["run", "list.txt", "-o", "ok.csv", "--failed", "bad.csv"]) == 2
    err = capsys.readouterr().err
    assert "blocking outgoing" in err
    assert not (env / "bad.csv").exists()
    with State("gmailcheck.db") as st:
        assert st.counts()["sent"] == 0


def test_test_command(env, monkeypatch, capsys):
    monkeypatch.setattr(sender, "_connect", lambda cfg: FakeSMTP())
    monkeypatch.setattr(imap, "connect", lambda cfg: FakeIMAP({"INBOX": []}))
    assert cli.main(["test", "--dir", str(env)]) == 0
    assert "All good" in capsys.readouterr().err


def test_junk_imap_host_is_ignored(tmp_path, monkeypatch):
    f = tmp_path / "accounts.csv"
    f.write_text("role,email,app_password,smtp_host,smtp_port,imap_host\n"
                 "master,m@gmail.com,aaaa bbbb,,,IMAP4rev1\n"
                 "sender,s@gmail.com,cccc dddd,,,IMAP4rev1\n")
    folder.apply_accounts(folder.load_accounts(f))
    assert os.environ["IMAP_HOST"] == "imap.gmail.com"
    assert os.environ["BOUNCE_IMAP_HOST"] == "imap.gmail.com"
    assert os.environ["SMTP_PRESET"] == "gmail"
