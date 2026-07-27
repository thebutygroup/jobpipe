"""The "your matches are ready" email.

The welcome email goes out the second someone signs up — before the instant
mini-run has scored anything — so it can only link to a page that is briefly
empty. This module closes the loop: once matches EXIST, send them.

Used two ways:
  - automatically: _instant_mini_run fires it when the mini-run found matches
  - manually: scripts/send_matches_email.py --user <ref>  (e.g. for signups
    whose original emails were lost before the signup_email audit existed)

Every attempt is recorded as a signup_email event (kind='matches_ready') so
"did they get it?" is answerable from the DB. Sent at most once per user
unless forced — nobody wants this twice.
"""

from __future__ import annotations

import logging

from .config import settings
from .db import log_event, tx

log = logging.getLogger(__name__)

KIND = "matches_ready"


def already_sent(conn, user_ref: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM events WHERE event_type='signup_email'"
        " AND json_extract(payload_json,'$.kind') = ?"
        " AND json_extract(payload_json,'$.user_ref') = ?"
        " AND json_extract(payload_json,'$.ok') = 1 LIMIT 1",
        (KIND, user_ref)).fetchone() is not None


def top_matches(conn, applicant_id: int, limit: int = 5) -> list[dict]:
    rows = conn.execute(
        "SELECT c.name AS company, p.title, p.id AS posting_id, m.score"
        " FROM matches m"
        " JOIN postings p ON p.id = m.posting_id"
        " JOIN companies c ON c.id = p.company_id"
        " WHERE m.applicant_id = ? AND m.score >= ?"
        " ORDER BY m.score DESC, m.created_at DESC LIMIT ?",
        (applicant_id, settings.match_threshold, limit)).fetchall()
    return [dict(r) for r in rows]


def compose(user_ref: str, matches: list[dict], n_total: int) -> tuple[str, str, str]:
    """(subject, html, text) for the matches-ready email."""
    base = (settings.dashboard_base_url or "").rstrip("/")
    page = f"{base}/job_matches/{user_ref}"
    items_html = "".join(
        f"<li><b>{m['score']}/10</b> — {m['company']} — "
        f"<a href='{base}/job_matches/{user_ref}/{m['posting_id']}'>"
        f"{m['title']}</a></li>" for m in matches)
    items_text = "\n".join(
        f"  {m['score']}/10  {m['company']} — {m['title']}" for m in matches)
    subject = f"Your matches are ready — {n_total} so far 🌱"
    html = (
        f"<p>Good news, <b>{user_ref}</b> — matching has run and you have "
        f"<b>{n_total}</b> match{'es' if n_total != 1 else ''} so far. "
        f"The strongest:</p><ul>{items_html}</ul>"
        f"<p>All of them, with the full \"why it fits\": "
        f"<a href='{page}'>{page}</a></p>"
        f"<p>New jobs are matched to you twice a day — bookmark that page. "
        f"Reply to this email any time with more about what you're after "
        f"(skills, salary, deal-breakers) and your matching profile gets "
        f"sharper.</p>")
    text = (f"You have {n_total} match(es) so far:\n\n{items_text}\n\n"
            f"All matches: {page}\n\nNew jobs are matched twice a day.")
    return subject, html, text


def send_matches_ready(conn, applicant_row, limit: int = 5,
                       force: bool = False) -> str:
    """Send the matches-ready email to one applicant. Returns a human-readable
    outcome string; never raises. Records the attempt in the DB."""
    user_ref = applicant_row["user_ref"] or ""
    try:
        from .profile import load_applicant_profile
        profile = load_applicant_profile(applicant_row)
        email = (getattr(profile.identity, "email", "") or "").strip()
    except Exception as e:  # noqa: BLE001 - report, don't crash callers
        return f"{user_ref}: could not load profile ({e})"
    if not email:
        return f"{user_ref}: no email on their profile — nothing to send"
    if not force and already_sent(conn, user_ref):
        return f"{user_ref}: matches-ready email already sent (use force to resend)"
    matches = top_matches(conn, applicant_row["id"], limit=limit)
    if not matches:
        return f"{user_ref}: no matches at/above threshold yet — not sending"
    n_total = conn.execute(
        "SELECT COUNT(*) AS n FROM matches WHERE applicant_id = ? AND score >= ?",
        (applicant_row["id"], settings.match_threshold)).fetchone()["n"]
    subject, html, text = compose(user_ref, matches, n_total)
    from . import notify
    ok = notify.send_email(subject=subject, html_body=html, text_body=text,
                           to=email)
    with tx(conn):
        log_event(conn, "signup_email", payload={
            "kind": KIND, "user_ref": user_ref, "ok": ok, "to": email,
            "n_matches": n_total})
    log.info("matches-ready email user=%s to=%s sent=%s", user_ref, email, ok)
    return (f"{user_ref}: sent {n_total}-match email to {email}" if ok
            else f"{user_ref}: SEND FAILED to {email} — check SMTP logs")
