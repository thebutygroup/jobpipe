"""Startup catch-up: jobs that missed their slot (restart killed the threads
waiting on the pipeline lock) run once on boot, in pipeline order. Weekly
jobs are reported, never auto-run."""
import json

import pytest

from jobpipe import scheduler


def _beat(conn, job, hours_ago, ok=True):
    conn.execute(
        "INSERT INTO events (event_type, payload_json, created_at)"
        " VALUES ('heartbeat', ?, strftime('%Y-%m-%dT%H:%M:%S','now', ?))",
        (json.dumps({"job": job, "ok": ok, "detail": ""}), f"-{hours_ago} hours"))
    conn.commit()


@pytest.fixture()
def wired(conn, monkeypatch):
    """Point the scheduler's connect() at the test DB and record job runs."""
    class NoCloseConn:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            if name == "close":
                return lambda: None
            return getattr(self._real, name)

    monkeypatch.setattr(scheduler, "connect", lambda: NoCloseConn(conn))
    ran = []
    for name in ("job_poll", "job_match", "job_prepare", "job_publish",
                 "job_track"):
        monkeypatch.setattr(scheduler, name,
                            lambda n=name: ran.append(n.removeprefix("job_")))
    return ran


def test_catch_up_runs_stale_jobs_in_pipeline_order(conn, wired):
    _beat(conn, "poll", 2)          # fresh
    _beat(conn, "prefilter", 2)     # fresh
    _beat(conn, "publish", 40)      # stale (24h cadence + 12h grace = 36h)
    _beat(conn, "match", 40)        # stale
    _beat(conn, "prepare", 40)      # stale
    _beat(conn, "track", 0)         # fresh
    _beat(conn, "indexscan", 2)     # fresh
    _beat(conn, "digest", 2)        # fresh
    scheduler.catch_up_stale_jobs()
    assert wired == ["match", "prepare", "publish"]  # order, not beat order


def test_catch_up_skips_fresh_failed_and_never_ran(conn, wired):
    _beat(conn, "poll", 2)
    _beat(conn, "prefilter", 2)
    _beat(conn, "match", 3, ok=False)  # failed, not stale — no retry loop
    _beat(conn, "prepare", 2)
    _beat(conn, "publish", 2)
    _beat(conn, "track", 0)
    _beat(conn, "indexscan", 2)
    # digest: never ran — must not fire on a fresh install
    scheduler.catch_up_stale_jobs()
    assert wired == []


def test_catch_up_never_runs_weekly_jobs(conn, wired, caplog):
    _beat(conn, "poll", 2)
    _beat(conn, "prefilter", 2)
    _beat(conn, "match", 2)
    _beat(conn, "prepare", 2)
    _beat(conn, "publish", 2)
    _beat(conn, "track", 0)
    _beat(conn, "indexscan", 24 * 20)  # weeks stale
    _beat(conn, "digest", 24 * 20)     # weeks stale — emails users!
    with caplog.at_level("WARNING"):
        scheduler.catch_up_stale_jobs()
    assert wired == []
    assert "indexscan" in caplog.text and "digest" in caplog.text
