"""Preparation stage: MATCHED -> PREPARED -> PENDING_REVIEW.

For each MATCHED application: extract the form (static-first), resolve every
field, draft free text, persist answers_json, and queue for review. Forms that
yield zero fields statically are flagged needs_browser for the submitter
container (which has Playwright) to re-extract.
"""

from __future__ import annotations

import json
import logging

from ..config import settings
from ..db import connect, heartbeat, log_event, now, transition, tx
from ..matching.matcher import profile_summary
from ..models import FAILED, PENDING_REVIEW, PREPARED
from ..pollers.base import FetchError
from ..profile import load_applicant_profile
from .answers import resolve
from .forms import extract_from_url

log = logging.getLogger(__name__)


# Statuses that mean the apply URL itself no longer exists — not a transient
# fault. 404/410 = removed; 403 is deliberately NOT here (bot walls return it
# for pages that work fine in a browser).
DEAD_STATUSES = frozenset({404, 410})


def _retire_dead(conn, app, status: int) -> None:
    """The apply URL is gone: the posting is dead, so retire the application
    instead of leaving it MATCHED to fail again every run (that is how one
    stale posting became a permanent 'failure' on /health, forever).

    FAILED keeps a retry path (FAILED -> MATCHED) if the closure was wrong;
    closing the posting removes it from match pages, digests and future
    prefilters, and the poller reopens it (closed_at = NULL) if any source
    ever sees it live again — self-correcting in both directions."""
    with tx(conn):
        conn.execute("UPDATE applications SET review_notes = ?, updated_at = ?"
                     " WHERE id = ?",
                     (f"apply_url gone (HTTP {status})", now(), app["id"]))
        conn.execute("UPDATE postings SET closed_at = datetime('now')"
                     " WHERE id = ? AND closed_at IS NULL", (app["posting_id"],))
        log_event(conn, "prepare:dead_url", application_id=app["id"],
                  payload={"status": status, "posting_id": app["posting_id"]})
    transition(conn, app["id"], FAILED,
               payload={"reason": "apply_url_gone", "status": status})
    log.info("retired application %d: apply URL gone (HTTP %d), posting %d closed",
             app["id"], status, app["posting_id"])


def matched_apps(conn) -> list:
    return conn.execute(
        "SELECT a.id, a.posting_id, a.applicant_id, p.title, p.apply_url, p.description_text,"
        "       c.name AS company "
        "FROM applications a JOIN postings p ON p.id = a.posting_id "
        "JOIN companies c ON c.id = p.company_id WHERE a.state = 'MATCHED'"
    ).fetchall()


def prepare_one(conn, app, profile, client) -> str:
    fields = extract_from_url(app["apply_url"])
    if not fields:
        with tx(conn):
            conn.execute("UPDATE applications SET review_notes = ? , updated_at = ?"
                         " WHERE id = ?", ("needs_browser extraction", now(), app["id"]))
            log_event(conn, "prepare:needs_browser", application_id=app["id"])
        return "needs_browser"

    answers, summary = {}, profile_summary(profile)
    for f in fields:
        r = resolve(f, profile)
        if r.llm and client is not None:
            from .freetext import draft_answer

            r.value = draft_answer(client, summary, app["company"], app["title"],
                                   app["description_text"], f.label)
        answers[f.key] = {"label": f.label, "kind": f.kind, "required": f.required,
                          "options": f.options, "value": r.value, "source": r.source,
                          "llm": r.llm, "unknown": r.unknown}

    cover = ""
    if client is not None and any(f.kind == "textarea" and "cover" in f.label.lower()
                                  for f in fields):
        from .freetext import draft_cover_letter

        cover = draft_cover_letter(client, summary, app["company"], app["title"],
                                   app["description_text"])

    with tx(conn):
        conn.execute("UPDATE applications SET answers_json = ?, cover_letter_text = ?,"
                     " resume_variant = ?, updated_at = ? WHERE id = ?",
                     (json.dumps(answers), cover, profile.documents.resume_default,
                      now(), app["id"]))
    transition(conn, app["id"], PREPARED,
               payload={"fields": len(fields),
                        "unknowns": sum(1 for a in answers.values() if a["unknown"])})
    transition(conn, app["id"], PENDING_REVIEW)
    return "prepared"


def run(conn, client=None) -> dict:
    # Compliance spine: answers must come from the OWNING applicant's profile.
    _profiles: dict[int, object] = {}

    def profile_for(applicant_id: int):
        if applicant_id not in _profiles:
            row = conn.execute("SELECT * FROM applicants WHERE id = ?",
                               (applicant_id,)).fetchone()
            _profiles[applicant_id] = load_applicant_profile(row)
        return _profiles[applicant_id]
    if client is None and settings.anthropic_api_key:
        import anthropic

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    stats = {"prepared": 0, "needs_browser": 0, "failed": 0, "dead_url": 0}
    for app in matched_apps(conn):
        try:
            outcome = prepare_one(conn, app, profile_for(app["applicant_id"]), client)
            stats[outcome if outcome in stats else "failed"] += 1
        except FetchError as e:
            if e.status in DEAD_STATUSES:
                _retire_dead(conn, app, e.status)
                stats["dead_url"] += 1
            else:
                stats["failed"] += 1
                log.warning("prepare fetch failed for application %d: %s",
                            app["id"], e)
        except Exception:
            stats["failed"] += 1
            log.exception("prepare failed for application %d", app["id"])
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        stats = run(conn)
        heartbeat(conn, "prepare", ok=True, detail=str(stats))
        log.info("prepare complete: %s", stats)
    except Exception as e:
        heartbeat(conn, "prepare", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
