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
