"""Scheduler container entrypoint: APScheduler running the pipeline jobs.

One process, no web server — the dashboard is served by gunicorn in the `web`
container (jobpipe.dashboard.wsgi). This matches the stack convention of one
process per container (like the bot's poller split out from the web process).

Schedule (Europe/London):
  poll      06:00 & 18:00   (prefilter runs immediately after each poll)
  match     06:45
  prepare   07:15
  publish   08:00
  track     hourly at :20
  indexscan Sundays 05:00
  digest    Mondays 08:30
"""

from __future__ import annotations

import logging
import signal
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from .config import settings
from .db import connect, heartbeat
from .notify import send_failure

log = logging.getLogger(__name__)

# Pipeline jobs share one SQLite file — run them one at a time. Without this,
# a long 06:00 poll (first run of new searches can take 45+ min) overlaps the
# 06:45 match and they fight over the write lock. The lock makes late jobs
# WAIT instead; combined with the 60s busy_timeout, 'database is locked'
# should never page again.
#
# WHY A PRIORITY LOCK, NOT threading.Lock: a plain Lock hands off to waiting
# threads in ARBITRARY order. On 17 Aug 2026 a 3.5h poll queued match (06:45),
# prepare, publish AND the Monday digest (08:30) behind it — and the digest
# won the handoff, composing "nothing new" emails from a day whose match run
# hadn't happened yet. Users with fresh score-7+ matches got skipped for a
# week. When several pipeline jobs are waiting, they must drain in PIPELINE
# order, because the later stages consume what the earlier ones produce.
_JOB_RANK = {"poll": 0, "prefilter": 1, "match": 2, "prepare": 3,
             "publish": 4, "digest": 5, "track": 6, "indexscan": 7}


class _PriorityLock:
    """Mutual exclusion where, among WAITING acquirers, the lowest rank goes
    first. Uncontended behaviour is identical to threading.Lock."""

    def __init__(self):
        self._cond = threading.Condition()
        self._held = False
        self._waiting: list[int] = []

    def try_acquire(self, rank: int) -> bool:
        with self._cond:
            if self._held or self._waiting:
                return False
            self._held = True
            return True

    def acquire(self, rank: int) -> None:
        with self._cond:
            self._waiting.append(rank)
            while self._held or min(self._waiting) < rank:
                self._cond.wait()
            self._waiting.remove(rank)
            self._held = True

    def release(self) -> None:
        with self._cond:
            self._held = False
            self._cond.notify_all()


_pipeline_lock = _PriorityLock()


def _guarded(job_name: str, fn) -> None:
    rank = _JOB_RANK.get(job_name, max(_JOB_RANK.values()) + 1)
    waited = not _pipeline_lock.try_acquire(rank)
    if waited:
        log.info("job %s waiting: another pipeline job is still running", job_name)
        _pipeline_lock.acquire(rank)
    try:
        fn()
    except Exception as e:
        log.exception("job %s failed", job_name)
        send_failure(job_name, str(e))
        try:
            conn = connect()
            heartbeat(conn, job_name, ok=False, detail=str(e))
            conn.close()
        except Exception:
            log.exception("could not record failure heartbeat for %s", job_name)
    finally:
        _pipeline_lock.release()


def job_poll() -> None:
    from .pollers import runner

    _guarded("poll", runner.main)
    from .matching import prefilter

    _guarded("prefilter", prefilter.main)


def job_match() -> None:
    from .matching import matcher

    _guarded("match", matcher.main)


def job_prepare() -> None:
    from .prepare import preparer

    _guarded("prepare", preparer.main)


def job_publish() -> None:
    from . import publish

    _guarded("publish", publish.main)


def job_indexscan() -> None:
    from .indexscan import runner as indexscan_runner

    _guarded("indexscan", indexscan_runner.main)


def job_track() -> None:
    from .track import confirm

    _guarded("track", confirm.main)


def job_digest() -> None:
    from . import digest

    _guarded("digest", digest.main)


_CATCHUP_ORDER = ("poll", "match", "prepare", "publish", "track")


def catch_up_stale_jobs() -> None:
    """Run, once, any pipeline job that missed its slot — in pipeline order.

    Why: a job that fires while another holds the pipeline lock waits in a
    thread; if the container restarts during that wait (crash, rebuild, a
    16-hour poll getting killed), the waiting jobs simply vanish and nothing
    reruns them until tomorrow. That is how 12 Aug 2026 became a day with no
    match/prepare/publish at all. On startup we consult the same staleness
    verdicts /health shows and run what was missed.

    Only 'stale' jobs are caught up: a job whose last run FAILED would retry
    (and re-email) on every restart, and 'never ran' on a fresh install would
    fire the whole pipeline before .env is even proven right. Weekly jobs
    (indexscan, digest — the latter emails users) are never auto-run; they get
    a log line telling the operator to run them manually if wanted."""
    from . import health

    jobs = {"poll": job_poll, "match": job_match, "prepare": job_prepare,
            "publish": job_publish, "track": job_track}
    try:
        conn = connect()
        board = {e["job"]: e for e in health.job_board(conn)}
        conn.close()
    except Exception:
        log.exception("catch-up: could not read the health board — skipping")
        return
    for j in ("indexscan", "digest"):
        e = board.get(j) or {}
        if e.get("status") == health.DOWN:
            log.warning("catch-up: weekly job %s is down (%s) — not auto-run, "
                        "start it manually if wanted", j, e.get("note", ""))
    stale = [j for j in _CATCHUP_ORDER
             if (board.get(j) or {}).get("status") == health.DOWN
             and "stale" in (board[j].get("note") or "")]
    if not stale:
        log.info("catch-up: nothing missed")
        return
    log.info("catch-up: running missed jobs in order: %s", ", ".join(stale))
    for j in stale:
        jobs[j]()  # _guarded inside handles heartbeat, lock and failure email


def start_scheduler() -> BackgroundScheduler:
    # coalesce: several missed firings collapse into one. misfire_grace_time:
    # a firing delayed up to an hour (slow wakeup, restart) still runs instead
    # of being silently discarded (APScheduler's default grace is 1 second).
    sched = BackgroundScheduler(
        timezone=settings.tz,
        job_defaults={"coalesce": True, "misfire_grace_time": 3600,
                      "max_instances": 1})
    sched.add_job(job_poll, "cron", hour="6,18", minute=0, id="poll")
    sched.add_job(job_match, "cron", hour=6, minute=45, id="match")
    sched.add_job(job_prepare, "cron", hour=7, minute=15, id="prepare")
    sched.add_job(job_publish, "cron", hour=8, minute=0, id="publish")
    sched.add_job(job_track, "cron", minute=20, id="track")
    sched.add_job(job_indexscan, "cron", day_of_week="sun", hour=5, minute=0,
                  id="indexscan")
    # After Monday's 06:45 match, so the week's first run is already in it.
    sched.add_job(job_digest, "cron", day_of_week="mon", hour=8, minute=30,
                  id="digest")
    sched.start()
    return sched


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    connect().close()  # create schema up-front
    start_scheduler()
    log.info("jobpipe scheduler started (tz=%s)", settings.tz)
    threading.Thread(target=catch_up_stale_jobs, name="catch-up",
                     daemon=True).start()
    # Block forever; respond to SIGTERM cleanly so `docker compose down` is quick.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    log.info("scheduler shutting down")


if __name__ == "__main__":
    main()
