from jobpipe.db import upsert_posting
from jobpipe.models import PostingDTO
from jobpipe.track import confirm


def _seed_submitted(conn, company="Acme"):
    conn.execute("INSERT INTO applicants (name, profile_path) VALUES ('t','p')")
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name=company, source="ats", external_id="1", title="SDE",
        location="London", apply_url=f"https://x/{company}"))
    conn.execute("INSERT INTO applications (posting_id, applicant_id, state, submitted_at,"
                 " created_at, updated_at) VALUES (?,1,'SUBMITTED',datetime('now'),"
                 "datetime('now'),datetime('now'))", (pid,))
    conn.commit()


def test_confirmation_matches_and_ignores_newsletters(conn):
    _seed_submitted(conn, "Acme")
    messages = [
        ("noreply@acme.com", "We've received your application", "<id-1>", ""),
        ("news@other.com", "Weekly newsletter: Acme raises Series B", "<id-2>", ""),
    ]
    n = confirm.match_and_confirm(conn, messages)
    assert n == 1
    state = conn.execute("SELECT state, confirmation_msg_id FROM applications").fetchone()
    assert state["state"] == "CONFIRMED" and state["confirmation_msg_id"] == "<id-1>"


def test_newsletter_alone_confirms_nothing(conn):
    _seed_submitted(conn, "Acme")
    n = confirm.match_and_confirm(conn, [
        ("news@acme.com", "Acme product update — no application here", "<id-9>", "")])
    assert n == 0


# ---- digest opt-out: "reply STOP" has to actually do something ---------------------

def _seed_applicant_with_email(conn, user_ref, addr):
    import yaml
    profile_yaml = yaml.safe_dump({
        "identity": {"full_name": user_ref, "email": addr, "location": "London"},
        "preferences": {"target_titles": ["Data Engineer"], "locations_ok": ["London"]},
    })
    conn.execute("INSERT INTO applicants (name, user_ref, profile_path, profile_yaml,"
                 " active) VALUES (?,?,'',?,1)", (user_ref, user_ref, profile_yaml))
    conn.commit()


def test_stop_reply_opts_that_user_out(conn):
    _seed_applicant_with_email(conn, "alice", "alice@example.com")
    _seed_applicant_with_email(conn, "bob", "bob@example.com")
    n = confirm.process_opt_outs(conn, [
        ("Alice <alice@example.com>", "Re: 3 new matches this week", "<id>", "STOP"),
    ])
    assert n == 1
    flags = dict(conn.execute("SELECT user_ref, digest_opt_out FROM applicants").fetchall())
    assert flags["alice"] == 1
    assert flags["bob"] == 0        # a STOP opts out the sender, nobody else


def test_stop_must_be_the_whole_reply(conn):
    """A quoted job title containing 'stop' must not unsubscribe anyone."""
    _seed_applicant_with_email(conn, "alice", "alice@example.com")
    n = confirm.process_opt_outs(conn, [
        ("alice@example.com", "Re: matches", "<id>",
         "Please stop sending me the ones in Leeds, the rest are great"),
        ("alice@example.com", "Bus Stop Inspector at Acme", "<id2>", ""),
    ])
    assert n == 0
    assert conn.execute("SELECT digest_opt_out FROM applicants"
                        ).fetchone()["digest_opt_out"] == 0


def test_unsubscribe_is_honoured_too(conn):
    _seed_applicant_with_email(conn, "alice", "alice@example.com")
    assert confirm.process_opt_outs(conn, [
        ("alice@example.com", "Re: matches", "<id>", "Unsubscribe.")]) == 1


# ---- bounces: an NDR unconfirms the address it names -------------------------

GMAIL_NDR_BODY = """\
Address not found

Your message wasn't delivered to maya@example.com because the domain
example.com couldn't be found. Check for typos or unnecessary spaces and
try again.

LEARN MORE

The response was:
DNS Error: domain example.com not found
"""


def _confirm_addr(conn, user_ref):
    conn.execute("UPDATE applicants SET email_confirmed_at ="
                 " '2026-08-01T00:00:00' WHERE user_ref = ?", (user_ref,))
    conn.commit()


def test_gmail_ndr_unconfirms_the_named_applicant(conn):
    _seed_applicant_with_email(conn, "maya", "maya@example.com")
    _seed_applicant_with_email(conn, "alice", "alice@real.com")
    _confirm_addr(conn, "maya")
    _confirm_addr(conn, "alice")
    n = confirm.process_bounces(conn, [
        ("Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
         "Delivery Status Notification (Failure)", "<ndr-1>", GMAIL_NDR_BODY)])
    assert n == 1
    rows = {r["user_ref"]: r["email_confirmed_at"] for r in
            conn.execute("SELECT user_ref, email_confirmed_at FROM applicants")}
    assert rows["maya"] is None          # unconfirmed — out of the pipeline
    assert rows["alice"] is not None     # untouched
    ev = conn.execute("SELECT payload_json FROM events WHERE"
                      " event_type='email:BOUNCED'").fetchone()
    assert "maya@example.com" in ev["payload_json"]


def test_ordinary_mail_naming_an_address_is_not_a_bounce(conn):
    _seed_applicant_with_email(conn, "maya", "maya@example.com")
    _confirm_addr(conn, "maya")
    n = confirm.process_bounces(conn, [
        ("recruiter@acme.com", "Intro for maya", "<m-1>",
         "You should meet maya@example.com, she's great")])
    assert n == 0
    assert conn.execute("SELECT email_confirmed_at FROM applicants"
                        ).fetchone()["email_confirmed_at"] is not None


def test_own_sending_address_in_an_ndr_is_ignored(conn, monkeypatch):
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "smtp_user", "jobpipe@thebutygroup.com")
    _seed_applicant_with_email(conn, "owner", "jobpipe@thebutygroup.com")
    _confirm_addr(conn, "owner")
    n = confirm.process_bounces(conn, [
        ("mailer-daemon@googlemail.com", "Address not found", "<ndr-2>",
         "From: jobpipe@thebutygroup.com\nsome bounce with only our address")])
    assert n == 0


def test_already_unconfirmed_bounce_is_a_noop(conn):
    _seed_applicant_with_email(conn, "maya", "maya@example.com")  # never confirmed
    n = confirm.process_bounces(conn, [
        ("mailer-daemon@googlemail.com",
         "Delivery Status Notification (Failure)", "<ndr-3>", GMAIL_NDR_BODY)])
    assert n == 0  # nothing to unconfirm, no event spam
