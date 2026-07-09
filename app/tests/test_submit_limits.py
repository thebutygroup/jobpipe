
from jobpipe.db import upsert_posting
from jobpipe.models import PostingDTO
from jobpipe.submit import runner


def _seed_company_app(conn, submitted_at=None, cooldown_until=None):
    conn.execute("INSERT INTO applicants (name, profile_path) VALUES ('t','p')")
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name="Acme", source="ats", external_id="1", title="SDE",
        location="London", apply_url="https://boards.greenhouse.io/acme/1"))
    cid = conn.execute("SELECT company_id FROM postings WHERE id=?", (pid,)).fetchone()[0]
    if cooldown_until:
        conn.execute("UPDATE companies SET cooldown_until=? WHERE id=?",
                     (cooldown_until, cid))
    conn.execute("INSERT INTO applications (posting_id, applicant_id, state, submitted_at,"
                 " created_at, updated_at) VALUES (?,1,'APPROVED',?,datetime('now'),"
                 "datetime('now'))", (pid, submitted_at))
    conn.commit()
    return cid


def test_master_switch_off_blocks_everything(conn, monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "submit_enabled", False)
    cid = _seed_company_app(conn)
    ok, reason = runner.may_submit(conn, cid)
    assert not ok and "off" in reason.lower()


def test_daily_cap_enforced(conn, monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "submit_enabled", True)
    monkeypatch.setattr(settings, "max_submissions_per_day", 1)
    cid = _seed_company_app(conn, submitted_at="2999-01-01T00:00:00")  # counts as today-ish
    # add one already-submitted today
    conn.execute("UPDATE applications SET submitted_at = datetime('now')")
    conn.commit()
    ok, reason = runner.may_submit(conn, cid)
    assert not ok and "cap" in reason


def test_min_interval_enforced(conn, monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "submit_enabled", True)
    monkeypatch.setattr(settings, "max_submissions_per_day", 10)
    monkeypatch.setattr(settings, "submit_min_interval_s", 300)
    cid = _seed_company_app(conn)
    conn.execute("UPDATE applications SET submitted_at = datetime('now')")
    conn.commit()
    ok, reason = runner.may_submit(conn, cid)
    assert not ok and "interval" in reason


def test_company_cooldown_enforced(conn, monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "submit_enabled", True)
    monkeypatch.setattr(settings, "max_submissions_per_day", 10)
    monkeypatch.setattr(settings, "submit_min_interval_s", 0)
    cid = _seed_company_app(conn, cooldown_until="2999-01-01T00:00:00")
    ok, reason = runner.may_submit(conn, cid)
    assert not ok and "cooldown" in reason


def test_all_clear_allows(conn, monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "submit_enabled", True)
    monkeypatch.setattr(settings, "max_submissions_per_day", 10)
    monkeypatch.setattr(settings, "submit_min_interval_s", 0)
    cid = _seed_company_app(conn)
    ok, reason = runner.may_submit(conn, cid)
    assert ok and reason == ""
