import pytest

from jobpipe.db import IllegalTransition, transition, upsert_posting
from jobpipe.models import MATCHED, PostingDTO


def make_dto(**kw):
    base = dict(company_name="Acme", source="ats", external_id="123",
                title="Senior Data Engineer", location="London",
                apply_url="https://boards.greenhouse.io/acme/jobs/123?gh_src=abc&utm_source=x",
                description_text="desc")
    base.update(kw)
    return PostingDTO(**base)


def test_upsert_twice_one_row(conn):
    _, new1 = upsert_posting(conn, make_dto())
    _, new2 = upsert_posting(conn, make_dto())
    conn.commit()
    assert (new1, new2) == (True, False)
    assert conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"] == 1


def test_cross_channel_dedup_by_canonical_url(conn):
    ats = make_dto(source="ats",
                   apply_url="https://boards.greenhouse.io/acme/jobs/123?gh_src=aaa")
    bi = make_dto(source="builtin", external_id="/job/sde-acme",
                  apply_url="https://boards.greenhouse.io/acme/jobs/123?utm_source=builtin")
    upsert_posting(conn, ats)
    upsert_posting(conn, bi)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"] == 1


def test_fallback_identity_when_no_url(conn):
    a = make_dto(apply_url="", source="ats")
    b = make_dto(apply_url="", source="builtin", external_id="other")
    upsert_posting(conn, a)
    upsert_posting(conn, b)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"] == 1


def _seed_application(conn, state="DISCOVERED"):
    conn.execute("INSERT INTO applicants (name, profile_path) VALUES ('t','p')")
    pid, _ = upsert_posting(conn, make_dto())
    conn.execute(
        "INSERT INTO applications (posting_id, applicant_id, state, created_at, updated_at)"
        " VALUES (?, 1, ?, datetime('now'), datetime('now'))", (pid, state))
    conn.commit()
    return conn.execute("SELECT id FROM applications").fetchone()["id"]


def test_illegal_transition_raises(conn):
    app_id = _seed_application(conn, state="DISCOVERED")
    with pytest.raises(IllegalTransition):
        transition(conn, app_id, MATCHED)  # DISCOVERED -> MATCHED skips PREFILTERED


def test_transition_writes_event(conn):
    app_id = _seed_application(conn, state="DISCOVERED")
    transition(conn, app_id, "PREFILTERED", payload={"why": "test"})
    ev = conn.execute(
        "SELECT * FROM events WHERE event_type='state:PREFILTERED'").fetchone()
    assert ev is not None and ev["application_id"] == app_id


def test_connect_sets_lock_patience(tmp_path):
    """'database is locked' regression: writers must WAIT (60s), not fail
    after python's default 5s."""
    from jobpipe.db import connect
    c = connect(str(tmp_path / "t.db"))
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 60000
    c.close()


def test_scheduler_serializes_pipeline_jobs():
    """Two _guarded jobs never run concurrently — the second waits."""
    import threading
    from jobpipe import scheduler

    order = []
    gate = threading.Event()

    def slow():
        order.append("slow-start")
        gate.wait(timeout=5)
        order.append("slow-end")

    def fast():
        order.append("fast")

    t1 = threading.Thread(target=scheduler._guarded, args=("slow", slow))
    t2 = threading.Thread(target=scheduler._guarded, args=("fast", fast))
    t1.start()
    while "slow-start" not in order:
        pass
    t2.start()
    import time
    time.sleep(0.2)
    assert "fast" not in order      # fast is blocked behind slow
    gate.set()
    t1.join()
    t2.join()
    assert order == ["slow-start", "slow-end", "fast"]
