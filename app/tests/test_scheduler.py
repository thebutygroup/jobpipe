"""Startup catch-up: jobs that missed their slot (restart killed the threads
waiting on the pipeline lock) run once on boot, in pipeline order. Weekly
jobs are reported, never auto-run."""
import json
import threading
import time

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


# ---- priority lock: waiting jobs drain in pipeline order --------------------
# Regression for 17 Aug 2026: a 3.5h poll queued match/prepare/publish/digest
# behind a plain threading.Lock, and the DIGEST won the arbitrary handoff —
# mailing "nothing new" from a day whose match run hadn't happened yet.

def test_waiting_jobs_run_in_pipeline_order(monkeypatch):
    lock = scheduler._PriorityLock()
    monkeypatch.setattr(scheduler, "_pipeline_lock", lock)
    lock.acquire(scheduler._JOB_RANK["poll"])  # the long poll holds the lock

    ran, threads = [], []
    # arrival order is deliberately the WORST case: reverse pipeline order
    for name in ("digest", "publish", "prepare", "match"):
        t = threading.Thread(
            target=scheduler._guarded, args=(name, lambda n=name: ran.append(n)))
        t.start()
        threads.append(t)
        # ensure this thread is queued before the next arrives
        for _ in range(200):
            if len(lock._waiting) == len(threads):
                break
            time.sleep(0.01)

    lock.release()  # poll finishes
    for t in threads:
        t.join(timeout=5)
    assert ran == ["match", "prepare", "publish", "digest"]


def test_priority_lock_uncontended_and_late_low_rank(monkeypatch):
    lock = scheduler._PriorityLock()
    monkeypatch.setattr(scheduler, "_pipeline_lock", lock)
    # uncontended: behaves like a plain lock
    ran = []
    scheduler._guarded("track", lambda: ran.append("track"))
    assert ran == ["track"]

    # a low-rank job arriving LATE still goes before a queued high-rank one
    lock.acquire(scheduler._JOB_RANK["poll"])
    t_track = threading.Thread(
        target=scheduler._guarded, args=("track", lambda: ran.append("track2")))
    t_track.start()
    for _ in range(200):
        if lock._waiting:
            break
        time.sleep(0.01)
    t_match = threading.Thread(
        target=scheduler._guarded, args=("match", lambda: ran.append("match")))
    t_match.start()
    for _ in range(200):
        if len(lock._waiting) == 2:
            break
        time.sleep(0.01)
    lock.release()
    t_track.join(timeout=5)
    t_match.join(timeout=5)
    assert ran == ["track", "match", "track2"]
