"""Submitter loop — runs in the dedicated Playwright container.

Every cycle: (1) browser-extract any applications flagged needs_browser,
(2) process APPROVED applications under hard rate limits.

Rate limits (checked in code, not just config):
- SUBMIT_ENABLED master switch (default OFF): approvals queue but nothing fires.
- MAX_SUBMISSIONS_PER_DAY, SUBMIT_MIN_INTERVAL_S between submissions,
  COMPANY_COOLDOWN_DAYS per company.
"""

from __future__ import annotations

import json
import logging
import time

from ..config import settings
from ..db import connect, heartbeat, log_event, now, transition, tx
from ..models import FAILED, NEEDS_HUMAN, SUBMITTED, SUBMITTING
from ..notify import send_email

log = logging.getLogger(__name__)


# ---- rate limiting (pure functions, unit-tested) --------------------------------------

def submissions_today(conn) -> int:
    return conn.execute("SELECT COUNT(*) c FROM applications "
                        "WHERE submitted_at >= date('now')").fetchone()["c"]


def seconds_since_last_submission(conn) -> float:
    row = conn.execute("SELECT MAX(submitted_at) m FROM applications").fetchone()
    if not row["m"]:
        return float("inf")
    import datetime as dt

    last = dt.datetime.fromisoformat(row["m"])
    return (dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - last).total_seconds()


def company_in_cooldown(conn, company_id: int) -> bool:
    row = conn.execute("SELECT cooldown_until FROM companies WHERE id = ?",
                       (company_id,)).fetchone()
    return bool(row and row["cooldown_until"] and row["cooldown_until"] > now())


def may_submit(conn, company_id: int) -> tuple[bool, str]:
    if not settings.submit_enabled:
        return False, "SUBMIT_ENABLED is off"
    if submissions_today(conn) >= settings.max_submissions_per_day:
        return False, "daily submission cap reached"
    if seconds_since_last_submission(conn) < settings.submit_min_interval_s:
        return False, "min interval between submissions not elapsed"
    if company_in_cooldown(conn, company_id):
        return False, "company in cooldown"
    return True, ""


# ---- the loop --------------------------------------------------------------------------

def approved_apps(conn) -> list:
    return conn.execute(
        "SELECT a.id, a.answers_json, a.posting_id, p.company_id, p.apply_url,"
        "       p.title, c.name AS company "
        "FROM applications a JOIN postings p ON p.id = a.posting_id "
        "JOIN companies c ON c.id = p.company_id "
        "WHERE a.state = 'APPROVED' ORDER BY a.updated_at ASC"
    ).fetchall()


def process_one(conn, app) -> str:
    ok, reason = may_submit(conn, app["company_id"])
    if not ok:
        log.info("holding application %d: %s", app["id"], reason)
        return "held"
    transition(conn, app["id"], SUBMITTING)
    answers = json.loads(app["answers_json"] or "{}")
    screenshot = f"/app/data/screenshots/app_{app['id']}.png"

    from . import browser, llm_fallback

    def unmapped_handler(page, unfilled, ans):
        return llm_fallback.run(page, unfilled, ans)

    try:
        result = browser.run_application(dict(app), answers, dry_run=False,
                                         screenshot_path=screenshot,
                                         unmapped_handler=unmapped_handler)
    except Exception as e:
        transition(conn, app["id"], FAILED, payload={"error": str(e)})
        send_email(f"[jobpipe] submission FAILED: {app['company']}",
                   f"<p>{app['title']}: {e}</p>")
        return "failed"

    if result["outcome"] == "submitted":
        with tx(conn):
            conn.execute("UPDATE applications SET submitted_at = ?, screenshot_path = ?"
                         " WHERE id = ?", (now(), screenshot, app["id"]))
            conn.execute("UPDATE companies SET cooldown_until = "
                         "datetime('now', ?) WHERE id = ?",
                         (f"+{settings.company_cooldown_days} days", app["company_id"]))
        transition(conn, app["id"], SUBMITTED)
        send_email(f"[jobpipe] submitted: {app['company']} — {app['title']}",
                   f"<p>Submitted with screenshot proof.</p>"
                   f"<p><a href='{settings.dashboard_base_url}/app/{app['id']}'>details</a></p>")
        return "submitted"

    # blocked: captcha / login / unmapped / verification diff / no confirmation
    transition(conn, app["id"], NEEDS_HUMAN, payload={"reason": result["reason"]})
    send_email(f"[jobpipe] needs you: {app['company']} — {app['title']}",
               f"<p>Paused: {result['reason']}</p>"
               f"<p><a href='{settings.dashboard_base_url}/app/{app['id']}'>open</a> — "
               f"fix, then hit “Fixed it — resume”.</p>")
    log_event(conn, "submit:paused", application_id=app["id"],
              payload={"reason": result["reason"]})
    conn.commit()
    return "needs_human"


def browser_extract_flagged(conn) -> int:
    """Re-extract forms that static extraction couldn't see (JS-rendered)."""
    rows = conn.execute("SELECT a.id, p.apply_url FROM applications a "
                        "JOIN postings p ON p.id = a.posting_id "
                        "WHERE a.state='MATCHED' AND a.review_notes='needs_browser extraction'"
                        ).fetchall()
    done = 0
    for row in rows:
        try:
            from playwright.sync_api import sync_playwright

            from ..prepare.forms import extract_from_html

            with sync_playwright() as pw:
                b = pw.chromium.launch(headless=True)
                page = b.new_page()
                page.goto(row["apply_url"], wait_until="networkidle", timeout=60000)
                fields = extract_from_html(page.content())
                b.close()
            if fields:
                with tx(conn):
                    conn.execute("UPDATE applications SET review_notes = '' , updated_at=?"
                                 " WHERE id = ?", (now(), row["id"]))
                # preparer picks it up next cycle via cached extraction? Simplest:
                # store the spec so preparer skips re-fetch.
                with tx(conn):
                    log_event(conn, "prepare:browser_extracted", application_id=row["id"],
                              payload={"fields": len(fields)})
                done += 1
        except Exception:
            log.exception("browser extraction failed for application %d", row["id"])
    return done


def cycle() -> dict:
    conn = connect()
    stats = {"submitted": 0, "needs_human": 0, "failed": 0, "held": 0, "extracted": 0}
    try:
        stats["extracted"] = browser_extract_flagged(conn)
        for app in approved_apps(conn):
            outcome = process_one(conn, app)
            stats[outcome] += 1
            if outcome == "submitted":
                time.sleep(settings.submit_min_interval_s)
        heartbeat(conn, "submit", ok=True, detail=str(stats))
    except Exception as e:
        heartbeat(conn, "submit", ok=False, detail=str(e))
        raise
    finally:
        conn.close()
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    interval = 30 * 60
    log.info("submitter loop starting (SUBMIT_ENABLED=%s)", settings.submit_enabled)
    while True:
        try:
            log.info("cycle: %s", cycle())
        except Exception:
            log.exception("cycle failed")
        time.sleep(interval)


if __name__ == "__main__":
    main()
