"""Folder mode: accounts.csv + emails/ in, success/ and failed/ out."""

import os

import pytest

from gmailcheck import cli, folder, imap, sender
from gmailcheck.config import ConfigError
from gmailcheck.state import State

from .fakes import FakeIMAP, FakeSMTP, forwarded

ENV_KEYS = ("IMAP_USER", "IMAP_PASS", "IMAP_HOST", "SMTP_USER", "SMTP_PASS", "SMTP_FROM",
            "SMTP_PRESET", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY",
            "BOUNCE_IMAP_USER", "BOUNCE_IMAP_PASS", "BOUNCE_IMAP_HOST")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(sender.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)


def test_first_run_creates_layout(tmp_path):
    assert cli.main(["folder", "--dir", str(tmp_path)]) == 1
    for name in ("emails", "success", "failed", "data"):
        assert (tmp_path / name).is_dir()
    assert (tmp_path / "accounts.csv").read_text().startswith("role,email,app_password")


def test_accounts_gmail_and_custom_smtp(tmp_path):
    f = tmp_path / "accounts.csv"
    f.write_text("role,email,app_password,smtp_host,smtp_port,imap_host\n"
                 "master,m@gmail.com,aaaa bbbb,,,\n"
                 "sender,s@gmail.com,cccc dddd,,,\n")
    folder.apply_accounts(folder.load_accounts(f))
    assert os.environ["SMTP_PRESET"] == "gmail"
    assert os.environ["BOUNCE_IMAP_USER"] == "s@gmail.com"

    f.write_text("role,email,app_password,smtp_host,smtp_port,imap_host\n"
                 "master,m@gmail.com,aaaa bbbb,,,\n"
                 "sender,check@mydomain.com,pw,mail.mydomain.com,465,\n")
    folder.apply_accounts(folder.load_accounts(f))
    assert "SMTP_PRESET" not in os.environ
    assert (os.environ["SMTP_HOST"], os.environ["SMTP_SECURITY"]) == ("mail.mydomain.com", "ssl")
    assert "BOUNCE_IMAP_USER" not in os.environ


def test_missing_sender_is_an_error(tmp_path):
    f = tmp_path / "accounts.csv"
    f.write_text("role,email,app_password\nmaster,m@gmail.com,pw\n")
    with pytest.raises(ConfigError, match="sender"):
        folder.apply_accounts(folder.load_accounts(f))


def test_folder_run_end_to_end(tmp_path, monkeypatch):
    (tmp_path / "emails").mkdir()
    (tmp_path / "emails" / "a.txt").write_text("one@gmail.com\ntwo@gmail.com\n")
    (tmp_path / "emails" / "b.csv").write_text("email\nthree@gmail.com\none@gmail.com\n")
    (tmp_path / "accounts.csv").write_text(
        "role,email,app_password,smtp_host,smtp_port,imap_host\n"
        "master,master@gmail.com,aaaa bbbb cccc dddd,,,\n"
        "sender,checker@gmail.com,eeee ffff gggg hhhh,,,\n")

    smtp = FakeSMTP()
    monkeypatch.setattr(sender, "_connect", lambda cfg: smtp)

    def fake_connect(cfg):
        if cfg.user != "master@gmail.com":
            return FakeIMAP({"INBOX": []})
        with State(str(tmp_path / "data" / "gmailcheck.db")) as st:
            tok = {r.email: r.token for r in st.all()}
        mails = [forwarded(tok[e], e) for e in ("one@gmail.com", "three@gmail.com") if e in tok]
        return FakeIMAP({"[Gmail]/All Mail": mails}, roles={"\\All": "[Gmail]/All Mail"})
    monkeypatch.setattr(imap, "connect", fake_connect)

    assert cli.main(["folder", "--dir", str(tmp_path), "--delay", "0",
                     "--wait", "0", "--interval", "1"]) == 0
    assert len(smtp.sent) == 3
    assert (tmp_path / "success" / "forwarding.txt").read_text().splitlines() == \
        ["one@gmail.com", "three@gmail.com"]
    assert "one@gmail.com,forwarding" in (tmp_path / "success" / "forwarding.csv").read_text()
    assert "two@gmail.com,not_received" in (tmp_path / "failed" / "not_forwarding.csv").read_text()
