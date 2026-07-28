"""The weekly "new since last time" digest.

Until this existed there was no recurring channel to a user at all: the
welcome and matches-ready emails fire once each at signup and then jobpipe
goes silent forever, however many good matches turn up afterwards.

The rule that shapes everything here is: NOTHING NEW MEANS NO EMAIL. A weekly
send that arrives empty is how a useful email becomes spam, and unsubscribing
from jobpipe means unsubscribing from the whole product. So a digest only goes
out when there is something in it the user has not already been told about.

"Since last time" is anchored to the last email we actually managed to send
them (digest or matches-ready), not to a fixed 7-day window — if a week's send
fails, the next one covers both weeks rather than dropping matches on the
floor.

Every attempt is recorded as a signup_email event (kind='digest') so "did they
get it?" stays answerable from the DB, exactly like the other user mail.
"""

from __future__ import annotations

import logging

from .config import settings
from .db import log_event, now, tx

log = logging.getLogger(__name__)

KIND = "digest"
# How far back a user's FIRST digest reaches when they have no prior email to
# anchor to. Without a floor the first one would dump their entire history.
FIRST_DIGEST_WINDOW_DAYS = 7
# Re-send guard. The job is weekly; anything inside this window is a re-run,
# not the next issue.
RESEND_GUARD_DAYS = 6


def recipients(conn) -> list[dict]:
    """Users eligible for a digest, before checking whether they have news.

    Same gates as the matcher (active, email-confirmed, not shadow-banned)
    plus the opt-out. Someone who never confirmed their address must not be
    mailed — that is the whole point of the confirmation.
    """
    rows = conn.execute(
        "SELECT * FROM applicants"
        " WHERE active = 1"
        "   AND email_confirmed_at IS NOT NULL"
        "   AND COALESCE(shadow_banned, 0) = 0"
        "   AND COALESCE(digest_opt_out, 0) = 0"
        " ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def last_email_at(conn, user_ref: str) -> str | None:
    """When we last successfully mailed this user something match-related."""
    row = conn.execute(
        "SELECT MAX(created_at) AS t FROM events"
        " WHERE event_type = 'signup_email'"
        "   AND json_extract(payload_json,'$.kind') IN ('digest','matches_ready')"
        "   AND json_extract(payload_json,'$.user_ref') = ?"
        "   AND json_extract(payload_json,'$.ok') = 1",
        (user_ref,)).fetchone()
    return row["t"] if row and row["t"] else None


def sent_recently(conn, user_ref: str) -> bool:
    """Idempotence: a re-run of the weekly job must not mail twice."""
    return conn.execute(
        "SELECT 1 FROM events"
        " WHERE event_type = 'signup_email'"
        "   AND json_extract(payload_json,'$.kind') = ?"
        "   AND json_extract(payload_json,'$.user_ref') = ?"
        "   AND json_extract(payload_json,'$.ok') = 1"
        "   AND created_at >= datetime('now', ?) LIMIT 1",
        (KIND, user_ref, f"-{RESEND_GUARD_DAYS} days")).fetchone() is not None


def cutoff_for(conn, user_ref: str) -> str:
    since = last_email_at(conn, user_ref)
    if since:
        return since
    row = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S','now',?) AS t",
        (f"-{FIRST_DIGEST_WINDOW_DAYS} days",)).fetchone()
    return row["t"]


def new_matches(conn, applicant_id: int, since: str, limit: int = 8) -> list[dict]:
    """Matches at/above threshold first scored since `since`."""
    rows = conn.execute(
        "SELECT c.name AS company, p.title, p.location, p.id AS posting_id,"
        "       MAX(m.score) AS score"
        " FROM matches m"
        " JOIN postings p ON p.id = m.posting_id"
        " JOIN companies c ON c.id = p.company_id"
        " WHERE m.applicant_id = ? AND m.score >= ? AND m.created_at > ?"
        "   AND p.closed_at IS NULL"
        " GROUP BY p.id"
        " ORDER BY score DESC, MAX(m.created_at) DESC LIMIT ?",
        (applicant_id, settings.match_threshold, since, limit)).fetchall()
    return [dict(r) for r in rows]


def near_misses(conn, applicant_id: int, since: str, limit: int = 6) -> list[dict]:
    """Scored 4-6: not worth an application, worth a nudge about the profile."""
    rows = conn.execute(
        "SELECT c.name AS company, p.title, MAX(m.score) AS score"
        " FROM matches m"
        " JOIN postings p ON p.id = m.posting_id"
        " JOIN companies c ON c.id = p.company_id"
        " WHERE m.applicant_id = ? AND m.created_at > ? AND p.closed_at IS NULL"
        " GROUP BY p.id HAVING MAX(m.score) BETWEEN 4 AND 6"
        " ORDER BY score DESC LIMIT ?",
        (applicant_id, since, limit)).fetchall()
    return [dict(r) for r in rows]


def compose(user_ref: str, matches: list[dict], near: list[dict],
            edit_url: str) -> tuple[str, str, str]:
    """(subject, html, text). Mirrors matches_mail.compose's voice."""
    base = (settings.dashboard_base_url or "").rstrip("/")
    page = f"{base}/job_matches/{user_ref}"
    n = len(matches)
    subject = f"{n} new match{'es' if n != 1 else ''} this week 🌱"

    items_html = "".join(
        f"<li><b>{m['score']}/10</b> — {m['company']} — "
        f"<a href='{base}/job_matches/{user_ref}/{m['posting_id']}'>"
        f"{m['title']}</a>"
        f"{' · ' + m['location'] if m.get('location') else ''}</li>"
        for m in matches)
    items_text = "\n".join(
        f"  {m['score']}/10  {m['company']} — {m['title']}" for m in matches)

    near_html = near_text = ""
    if near:
        near_list = ", ".join(f"{x['company']} ({x['score']}/10)" for x in near)
        near_html = (
            f"<p style='color:#5B6B60'>Also close but not quite: {near_list}. "
            f"If those look more right than the ones above, your profile is "
            f"the dial — <a href='{edit_url}'>edit it here</a> and matching "
            f"follows.</p>")
        near_text = (f"\nClose but not quite: {near_list}\n"
                     f"Adjust your profile: {edit_url}\n")

    html = (
        f"<p>Since we last wrote, <b>{n}</b> new job"
        f"{'s' if n != 1 else ''} matched what you're after:</p>"
        f"<ul>{items_html}</ul>"
        f"<p>All of them, with the full \"why it fits\": "
        f"<a href='{page}'>{page}</a></p>"
        f"{near_html}"
        f"<p style='color:#5B6B60;font-size:13px'>You get this once a week, "
        f"and only when there's something new. Reply STOP to stop it.</p>")
    text = (
        f"Since we last wrote, {n} new job(s) matched what you're after:\n\n"
        f"{items_text}\n\nAll matches: {page}\n{near_text}\n"
        f"You get this once a week, and only when there's something new. "
        f"Reply STOP to stop it.")
    return subject, html, text


def send_one(conn, row: dict, dry_run: bool = False) -> str:
    """Digest one applicant. Returns a human-readable outcome; never raises."""
    user_ref = row["user_ref"] or ""
    try:
        from .profile import load_applicant_profile
        profile = load_applicant_profile(row)
        email = (getattr(profile.identity, "email", "") or "").strip()
    except Exception as e:  # noqa: BLE001 - report, don't crash the run
        return f"{user_ref}: could not load profile ({e})"
    if not email:
        return f"{user_ref}: no email on their profile — nothing to send"
    if sent_recently(conn, user_ref):
        return f"{user_ref}: already digested in the last {RESEND_GUARD_DAYS} days"

    since = cutoff_for(conn, user_ref)
    matches = new_matches(conn, row["id"], since)
    if not matches:
        return f"{user_ref}: nothing new since {since} — not sending"

    from .profile_edit import ensure_edit_token
    base = (settings.dashboard_base_url or "").rstrip("/")
    token = ensure_edit_token(conn, row["id"])
    edit_url = f"{base}/profile/{user_ref}/{token}"

    near = near_misses(conn, row["id"], since)
    subject, html, text = compose(user_ref, matches, near, edit_url)
    if dry_run:
        return (f"{user_ref}: WOULD send {len(matches)} new match(es) to {email} "
                f"(since {since}) — subject: {subject!r}")

    from . import notify
    ok = notify.send_email(subject=subject, html_body=html, text_body=text,
                           to=email)
    with tx(conn):
        log_event(conn, "signup_email", payload={
            "kind": KIND, "user_ref": user_ref, "ok": ok, "to": email,
            "n_new": len(matches), "since": since})
    log.info("digest user=%s to=%s new=%d sent=%s",
             user_ref, email, len(matches), ok)
    return (f"{user_ref}: sent {len(matches)} new match(es) to {email}" if ok
            else f"{user_ref}: SEND FAILED to {email} — check SMTP logs")


def run(conn, dry_run: bool = False) -> dict:
    stats = {"considered": 0, "sent": 0, "skipped": 0, "failed": 0}
    for row in recipients(conn):
        stats["considered"] += 1
        outcome = send_one(conn, row, dry_run=dry_run)
        if "SEND FAILED" in outcome:
            stats["failed"] += 1
        elif outcome.split(": ", 1)[-1].startswith(("sent", "WOULD send")):
            stats["sent"] += 1
        else:
            stats["skipped"] += 1
        log.info("digest: %s", outcome)
    return stats


def main(dry_run: bool = False) -> None:
    from .db import connect, heartbeat

    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        stats = run(conn, dry_run=dry_run)
        if not dry_run:
            heartbeat(conn, "digest", ok=True,
                      detail=f"sent={stats['sent']} skipped={stats['skipped']} "
                             f"failed={stats['failed']}")
        log.info("digest complete: %s at %s", stats, now())
    except Exception as e:
        if not dry_run:
            heartbeat(conn, "digest", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
