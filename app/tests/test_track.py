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
        ("noreply@acme.com", "We've received your application", "<id-1>"),
        ("news@other.com", "Weekly newsletter: Acme raises Series B", "<id-2>"),
    ]
    n = confirm.match_and_confirm(conn, messages)
    assert n == 1
    state = conn.execute("SELECT state, confirmation_msg_id FROM applications").fetchone()
    assert state["state"] == "CONFIRMED" and state["confirmation_msg_id"] == "<id-1>"


def test_newsletter_alone_confirms_nothing(conn):
    _seed_submitted(conn, "Acme")
    n = confirm.match_and_confirm(conn, [
        ("news@acme.com", "Acme product update — no application here", "<id-9>")])
    assert n == 0
