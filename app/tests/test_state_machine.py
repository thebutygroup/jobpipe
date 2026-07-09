import pytest

from jobpipe.db import IllegalTransition, transition, upsert_posting
from jobpipe.models import PostingDTO

FULL_PATH = ["PREFILTERED", "MATCHED", "PREPARED", "PENDING_REVIEW",
             "APPROVED", "SUBMITTING", "SUBMITTED", "CONFIRMED"]


def _app(conn):
    conn.execute("INSERT INTO applicants (name, profile_path) VALUES ('t','p')")
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name="Acme", source="ats", external_id="1", title="SDE",
        location="London", apply_url="https://x/1"))
    conn.execute("INSERT INTO applications (posting_id, applicant_id, state, created_at,"
                 " updated_at) VALUES (?,1,'DISCOVERED',datetime('now'),datetime('now'))",
                 (pid,))
    conn.commit()
    return conn.execute("SELECT id FROM applications").fetchone()["id"]


def test_happy_path_all_transitions_legal(conn):
    app_id = _app(conn)
    for state in FULL_PATH:
        transition(conn, app_id, state)
    assert conn.execute("SELECT state FROM applications WHERE id=?",
                        (app_id,)).fetchone()["state"] == "CONFIRMED"


def test_cannot_skip_review(conn):
    app_id = _app(conn)
    for state in ["PREFILTERED", "MATCHED", "PREPARED", "PENDING_REVIEW"]:
        transition(conn, app_id, state)
    with pytest.raises(IllegalTransition):
        transition(conn, app_id, "SUBMITTING")  # must go through APPROVED


def test_needs_human_can_resume(conn):
    app_id = _app(conn)
    for state in ["PREFILTERED", "MATCHED", "PREPARED", "PENDING_REVIEW",
                  "APPROVED", "SUBMITTING", "NEEDS_HUMAN"]:
        transition(conn, app_id, state)
    transition(conn, app_id, "SUBMITTING")  # resume is legal
    transition(conn, app_id, "SUBMITTED")
