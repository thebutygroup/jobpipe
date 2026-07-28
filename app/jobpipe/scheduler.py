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
_pipeline_lock = threading.Lock()


def _guarded(job_name: str, fn) -> None:
    waited = not _pipeline_lock.acquire(blocking=False)
    if waited:
        log.info("job %s waiting: another pipeline job is still running", job_name)
        _pipeline_lock.acquire()
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


def start_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=settings.tz)
    sched.add_job(job_poll, "cron", hour="6,18", minute=0, id="poll")
    sched.add_job(job_match, "cron", hour=6, minute=45, id="match")
    sched.add_job(job_prepare, "cron", hour=7, minute=15, id="prepare")
    sched.add_job(job_publish, "cron", hour=8, minute=0, id="publish")
    sched.add_job(job_track, "cron", minute=20, id="track")
    sched.add_job(job_indexscan, "cron", day_of_week="sun", hour=5, minute=0,
                  id="indexscan")
    sched.start()
    return sched


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    connect().close()  # create schema up-front
    start_scheduler()
    log.info("jobpipe scheduler started (tz=%s)", settings.tz)
    # Block forever; respond to SIGTERM cleanly so `docker compose down` is quick.
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    stop.wait()
    log.info("scheduler shutting down")


if __name__ == "__main__":
    main()
