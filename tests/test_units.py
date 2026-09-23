from datetime import timedelta

from gmailcheck import bounces, checker, config, export, inputs
from gmailcheck.sender import build_message
from gmailcheck.state import (BOUNCED, FORWARDING, NOT_RECEIVED, PENDING, SENT,
                              State)

from .fakes import FakeIMAP, delay_notice, forwarded, gmail_bounce


def test_load_txt(tmp_path):
    f = tmp_path / "list.txt"
    f.write_text("# my list\nA@Gmail.com\n\n b@gmail.com \na@gmail.com\nnot-an-email\n")
    r = inputs.load_emails(f)
    assert r.emails == ["a@gmail.com", "b@gmail.com"]
    assert r.duplicates == 1
    assert r.invalid == [(6, "not-an-email")]


def test_load_csv_with_header(tmp_path):
    f = tmp_path / "list.csv"
    f.write_text("name,Email\nAnn,ann@gmail.com\nBob,bob@gmail.com\n")
    assert inputs.load_emails(f).emails == ["ann@gmail.com", "bob@gmail.com"]


def test_load_csv_without_header(tmp_path):
    f = tmp_path / "list.csv"
    f.write_text("id,addr\n1,x@gmail.com\n2,y@gmail.com\n")
    assert inputs.load_emails(f).emails == ["x@gmail.com", "y@gmail.com"]


def test_state_lifecycle(tmp_path):
    with State(str(tmp_path / "s.db")) as st:
        assert st.add(["a@x.com", "b@x.com"]) == 2
        assert st.add(["a@x.com", "c@x.com"]) == 1
        assert [r.email for r in st.pending()] == ["a@x.com", "b@x.com", "c@x.com"]

        st.mark_sent("a@x.com")
        st.mark_sent("b@x.com")
        assert st.sent_in_last_24h() == 2
        assert st.mark_forwarding("a@x.com")
        assert not st.mark_forwarding("a@x.com")  # already forwarding

        assert st.finalise(timedelta(minutes=30)) == 0  # too recent
        assert st.finalise(timedelta(0)) == 1
        assert st.counts() == {PENDING: 1, SENT: 0, FORWARDING: 1, BOUNCED: 0, NOT_RECEIVED: 1}

        old_token = st.by_status(NOT_RECEIVED)[0].token
        assert st.requeue((NOT_RECEIVED,)) == 1
        b = [r for r in st.pending() if r.email == "b@x.com"][0]
        assert b.token != old_token and b.sent_at is None


def test_message_carries_token():
    msg = build_message("sender@example.com", "a@gmail.com", "GMC-ABCDEF1234")
    assert "GMC-ABCDEF1234" in msg["Subject"]
    assert msg["X-GmailCheck-Token"] == "GMC-ABCDEF1234"
    assert "GMC-ABCDEF1234" in msg.get_content()


def test_scan_master_finds_inbox_and_spam(tmp_path):
    conn = FakeIMAP(
        {"INBOX": [], "[Gmail]/All Mail": [forwarded("GMC-AAAAAAAAAA", "a@gmail.com"),
                                             forwarded("GMC-FFFFFFFFFF", "zz@gmail.com")],
         "[Gmail]/Spam": [forwarded("GMC-BBBBBBBBBB", "b@gmail.com")],
         "[Gmail]/Trash": []},
        roles={"\\All": "[Gmail]/All Mail", "\\Junk": "[Gmail]/Spam", "\\Trash": "[Gmail]/Trash"},
    )
    from datetime import datetime, timezone
    known = {"GMC-AAAAAAAAAA", "GMC-BBBBBBBBBB", "GMC-CCCCCCCCCC"}
    res = checker.scan_master(conn, datetime.now(timezone.utc), known)
    assert res.found == {"GMC-AAAAAAAAAA": "inbox", "GMC-BBBBBBBBBB": "spam"}
    assert checker.cleanup(conn, res) == 2
    assert {m[2] for m in conn.moved} == {'"[Gmail]/Trash"'}


def test_parse_gmail_bounce():
    b = bounces.parse_bounce(gmail_bounce("GMC-1234567890", "gone@gmail.com"))
    assert b is not None
    assert b.tokens == {"GMC-1234567890"}
    assert "gone@gmail.com" in b.recipients
    assert "does not exist" in b.reason


def test_delay_notice_is_not_a_bounce():
    assert bounces.parse_bounce(delay_notice("slow@gmail.com")) is None


def test_export_formats(tmp_path):
    with State(str(tmp_path / "s.db")) as st:
        st.add(["a@x.com", "b@x.com"])
        st.mark_sent("a@x.com")
        st.mark_forwarding("a@x.com")
        recs = st.by_status(FORWARDING)
    export.write(recs, tmp_path / "ok.txt")
    assert (tmp_path / "ok.txt").read_text() == "a@x.com\n"
    export.write(recs, tmp_path / "ok.csv")
    lines = (tmp_path / "ok.csv").read_text().splitlines()
    assert lines[0] == "email,status,sent_at,found_at,detail"
    assert lines[1].startswith("a@x.com,forwarding,")


def test_dotenv_and_gmail_preset(tmp_path, monkeypatch):
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "SMTP_FROM", "SMTP_PRESET",
              "SMTP_SECURITY", "IMAP_PASS"):
        monkeypatch.delenv(k, raising=False)
    env = tmp_path / ".env"
    env.write_text('SMTP_USER=me@gmail.com  # sender\nSMTP_PASS="abcd efgh ijkl mnop" # quoted\n'
                   'IMAP_PASS=wxyz wxyz\n')
    config.load_dotenv(env)
    cfg = config.smtp_config("gmail")
    assert (cfg.host, cfg.port, cfg.security) == ("smtp.gmail.com", 587, "starttls")
    assert cfg.password == "abcdefghijklmnop"
    assert cfg.sender == "me@gmail.com"
    assert config.master_imap_config().password == "wxyzwxyz"
