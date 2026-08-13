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
        "SELECT a.id, ap.user_ref, p.id AS posting_id, c.name AS company, p.title, p.location, m.score, a.answers_json, "
        "m.created_at AS matched_at "
        "FROM applications a JOIN postings p ON p.id = a.posting_id "
        "JOIN companies c ON c.id = p.company_id "
        "JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN matches m ON m.posting_id = a.posting_id AND m.applicant_id = a.applicant_id "
        "WHERE a.state IN ('PENDING_REVIEW','NEEDS_HUMAN') "
        "ORDER BY m.score DESC LIMIT 25").fetchall()


def last_publish_at(conn) -> str | None:
    """When the previous successful publish email went out — the 'new since'
    cutoff. Read BEFORE this run's heartbeat is written, so it's genuinely
    the previous morning's email, not this one."""
    row = conn.execute(
        "SELECT MAX(created_at) AS t FROM events"
        " WHERE event_type = 'heartbeat'"
        "   AND json_extract(payload_json,'$.job') = 'publish'"
        "   AND json_extract(payload_json,'$.ok') = 1").fetchone()
    return row["t"] if row and row["t"] else None


def recent_activity(conn, days: int = 7) -> list:
    """Matcher activity by day: what the pipeline actually did lately, so the
    email shows match HISTORY instead of looking identical every morning."""
    return conn.execute(
        "SELECT date(created_at) AS d, COALESCE(SUM(score >= ?), 0) AS hits,"
        "       COUNT(*) AS scored"
        " FROM matches WHERE created_at >= date('now', ?)"
        " GROUP BY d ORDER BY d DESC",
        (settings.match_threshold, f"-{days} days")).fetchall()


def _day(ts: str | None) -> str:
    return (ts or "")[:10] or "?"


def build_digest(conn) -> tuple[int, str, str]:
    rows = pending_for_review(conn)
    base = settings.dashboard_base_url
    cutoff = last_publish_at(conn) or ""
    fresh = [r for r in rows if (r["matched_at"] or "") > cutoff]
    older = [r for r in rows if (r["matched_at"] or "") <= cutoff]

    def li(r, new=False):
        answers = json.loads(r["answers_json"] or "{}")
        gaps = sum(1 for f in answers.values()
                   if f.get("required") and (f.get("unknown") or not f.get("value")))
        gap_txt = f" · <b style='color:#c60'>{gaps} to fill</b>" if gaps else ""
        tag = "<b style='color:#080'>NEW</b> " if new else ""
        return (f"<li>{tag}<a href='{base}/job_matches/{r['user_ref']}/{r['posting_id']}'>"
                f"{r['company']} — {r['title']}</a> "
                f"({r['score']}/10, {r['location']}, {r['user_ref']}, "
                f"matched {_day(r['matched_at'])}){gap_txt}</li>")

    banner = ("" if settings.submit_enabled else
              "<p style='background:#fff3e0;padding:8px;border-radius:4px'>"
              "⚠️ SUBMIT_ENABLED is OFF — approving queues an application but nothing "
              "is sent until you turn the master switch on.</p>")
    sections = []
    if fresh:
        sections.append(f"<p><b>New since the last email ({len(fresh)}):</b></p>"
                        f"<ul>{''.join(li(r, new=True) for r in fresh)}</ul>")
    if older:
        label = "Still waiting" if fresh else "Waiting (nothing new since the last email)"
        sections.append(f"<p><b>{label} ({len(older)}):</b></p>"
                        f"<ul>{''.join(li(r) for r in older)}</ul>")
    activity = recent_activity(conn)
    act_rows = "".join(
        f"<tr><td>{a['d']}</td><td align='right'>{a['hits']}</td>"
        f"<td align='right'>{a['scored']}</td></tr>" for a in activity) or \
        "<tr><td colspan='3'>no matcher runs in the last 7 days</td></tr>"
    act_html = ("<p><b>Matcher activity, last 7 days:</b></p>"
                "<table cellpadding='4'><tr><th>day</th><th>matches</th>"
                f"<th>scored</th></tr>{act_rows}</table>")
    html = (f"<p><b>{len(rows)}</b> application(s) waiting for your review"
            f"{f', <b>{len(fresh)}</b> new' if fresh else ''}. "
            f"Each opens its own page showing the exact details to be submitted; "
            f"the submit button lives there.</p>{banner}{''.join(sections)}"
            f"{act_html}<p><a href='{base}/'>Open the review queue</a></p>")

    def line(r, new=False):
        return (f"- {'NEW ' if new else ''}{r['company']} — {r['title']} "
                f"({r['score']}/10, {r['user_ref']}, matched {_day(r['matched_at'])}): "
                f"{base}/job_matches/{r['user_ref']}/{r['posting_id']}")

    text = "\n".join(
        [f"{len(rows)} application(s) waiting" +
         (f", {len(fresh)} new since the last email." if fresh else ", nothing new.")] +
        [line(r, new=True) for r in fresh] + [line(r) for r in older] +
        ["", "Matcher activity, last 7 days (day: matches / scored):"] +
        [f"  {a['d']}: {a['hits']} / {a['scored']}" for a in activity])
    return len(rows), len(fresh), html, text


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        n, n_new, html, text = build_digest(conn)
        subject = (f"[jobpipe] {n_new} new match(es) · {n} to review" if n_new
                   else f"[jobpipe] {n} to review, nothing new")
        sent = send_email(subject, html, text)
        heartbeat(conn, "publish", ok=True,
                  detail=json.dumps({"pending": n, "new": n_new, "sent": sent}))
        log.info("publish: %d pending (%d new), sent=%s", n, n_new, sent)
    except Exception as e:
        heartbeat(conn, "publish", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
