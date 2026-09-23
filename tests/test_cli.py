"""End-to-end CLI flow with fake SMTP and IMAP servers."""

import pytest

from gmailcheck import cli, imap, sender
from gmailcheck.state import State

from .fakes import FakeIMAP, FakeSMTP, forwarded, gmail_bounce


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for k, v in {"SMTP_HOST": "smtp.example.com", "SMTP_USER": "checker@example.com",
                 "SMTP_PASS": "x", "IMAP_USER": "master@gmail.com", "IMAP_PASS": "y",
                 "BOUNCE_IMAP_USER": "checker@example.com", "BOUNCE_IMAP_PASS": "x"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SMTP_PRESET", raising=False)
    monkeypatch.setattr(sender.time, "sleep", lambda s: None)
    (tmp_path / "list.txt").write_text(
        "alive@gmail.com\nspammy@gmail.com\nbroken@gmail.com\ngone@gmail.com\nrefused@gmail.com\n")
    return tmp_path


def test_full_flow(env, monkeypatch):
    smtp = FakeSMTP(refuse={"refused@gmail.com"})
    monkeypatch.setattr(sender, "_connect", lambda cfg: smtp)

    assert cli.main(["send", "list.txt", "--delay", "0"]) == 0
    assert len(smtp.sent) == 4

    with State("gmailcheck.db") as st:
        tok = {r.email: r.token for r in st.all()}

    master = FakeIMAP(
        {"[Gmail]/All Mail": [forwarded(tok["alive@gmail.com"], "alive@gmail.com")],
         "[Gmail]/Spam": [forwarded(tok["spammy@gmail.com"], "spammy@gmail.com")]},
        roles={"\\All": "[Gmail]/All Mail", "\\Junk": "[Gmail]/Spam"},
    )
    sender_box = FakeIMAP({"INBOX": [gmail_bounce(tok["gone@gmail.com"], "gone@gmail.com")]})
    monkeypatch.setattr(imap, "connect",
                        lambda cfg: master if cfg.user == "master@gmail.com" else sender_box)

    assert cli.main(["check", "--final", "--grace", "0"]) == 0
    assert cli.main(["export", "-o", "ok.txt", "--failed", "bad.csv"]) == 0

    assert (env / "ok.txt").read_text().splitlines() == ["alive@gmail.com", "spammy@gmail.com"]
    bad = (env / "bad.csv").read_text()
    assert "gone@gmail.com,bounced" in bad
    assert "refused@gmail.com,bounced" in bad
    assert "broken@gmail.com,not_received" in bad


def test_daily_cap_resumes(env, monkeypatch):
    smtp = FakeSMTP()
    monkeypatch.setattr(sender, "_connect", lambda cfg: smtp)
    assert cli.main(["send", "list.txt", "--delay", "0", "--daily-cap", "2"]) == 0
    assert len(smtp.sent) == 2
    # Cap reached: nothing more goes out until the 24h window moves.
    assert cli.main(["send", "--delay", "0", "--daily-cap", "2"]) == 0
    assert len(smtp.sent) == 2
    assert cli.main(["send", "--delay", "0", "--daily-cap", "10"]) == 0
    assert len(smtp.sent) == 5


def test_dry_run_sends_nothing(env, monkeypatch, capsys):
    monkeypatch.setattr(sender, "_connect", lambda cfg: pytest.fail("should not connect"))
    assert cli.main(["send", "list.txt", "--dry-run"]) == 0
    assert "Forward check GMC-" in capsys.readouterr().err
    with State("gmailcheck.db") as st:
        assert st.all() == []
