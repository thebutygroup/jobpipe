"""Daily publish job (08:00): email a digest where each match is a deep link to
its own review page. The submit action lives on that page, never in the email.
"""

from __future__ import annotations

import json
import logging

from .config import settings
from .db import connect, heartbeat
from .notify import send_email

log = logging.getLogger(__name__)


def pending_for_review(conn) -> list:
    return conn.execute(
        "SELECT a.id, ap.user_ref, p.id AS posting_id, c.name AS company, p.title, p.location, m.score, a.answers_json "
        "FROM applications a JOIN postings p ON p.id = a.posting_id "
        "JOIN companies c ON c.id = p.company_id "
        "JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN matches m ON m.posting_id = a.posting_id AND m.applicant_id = a.applicant_id "
        "WHERE a.state IN ('PENDING_REVIEW','NEEDS_HUMAN') "
        "ORDER BY m.score DESC LIMIT 25").fetchall()


def build_digest(conn) -> tuple[int, str, str]:
    rows = pending_for_review(conn)
    base = settings.dashboard_base_url
    items = []
    for r in rows:
        answers = json.loads(r["answers_json"] or "{}")
        gaps = sum(1 for f in answers.values()
                   if f.get("required") and (f.get("unknown") or not f.get("value")))
        gap_txt = f" · <b style='color:#c60'>{gaps} to fill</b>" if gaps else ""
        items.append(
            f"<li><a href='{base}/job_matches/{r['user_ref']}/{r['posting_id']}'>"
            f"{r['company']} — {r['title']}</a> "
            f"({r['score']}/10, {r['location']}){gap_txt}</li>")
    banner = ("" if settings.submit_enabled else
              "<p style='background:#fff3e0;padding:8px;border-radius:4px'>"
              "⚠️ SUBMIT_ENABLED is OFF — approving queues an application but nothing "
              "is sent until you turn the master switch on.</p>")
    html = (f"<p><b>{len(rows)}</b> application(s) waiting for your review. "
            f"Each opens its own page showing the exact details to be submitted; "
            f"the submit button lives there.</p>{banner}<ul>{''.join(items)}</ul>"
            f"<p><a href='{base}/'>Open the review queue</a></p>")
    text = f"{len(rows)} application(s) waiting.\n" + "\n".join(
        f"- {r['company']} — {r['title']} ({r['score']}/10): "
        f"{base}/job_matches/{r['user_ref']}/{r['posting_id']}"
        for r in rows)
    return len(rows), html, text


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        n, html, text = build_digest(conn)
        sent = send_email(f"[jobpipe] {n} application(s) to review", html, text)
        heartbeat(conn, "publish", ok=True, detail=json.dumps({"pending": n, "sent": sent}))
        log.info("publish: %d pending, sent=%s", n, sent)
    except Exception as e:
        heartbeat(conn, "publish", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
