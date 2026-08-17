"""The weekly digest — EVERY user hears from us on Monday (Joe, 17 Aug 2026).

The original rule was "nothing new means no email". Live experience flipped
it: silence reads as "the tool stopped working", and every segment has
something genuinely useful to hear. So each Monday:

- new score-7+ matches   -> the match digest (unchanged)
- only near-misses (4-6) -> those, framed as "a profile tweak could tip them"
- nothing at all         -> a quiet-week note: widen the titles, here's the dial
- never confirmed        -> a confirmation reminder (capped at 2, ever — a
                            weekly send to an unproven address is the classic
                            spam-trap pattern, and Gmail reputation is the
                            asset everything else depends on)

Still NEVER mailed: opted-out (STOP), shadow-banned, and bounce-unconfirmed
addresses (an email:BOUNCED event for their current address = the mailbox is
dead; mailing it again is a bounce loop, not a reminder).

"Since last time" is anchored to the last email we actually managed to send
them (digest or matches-ready), not to a fixed 7-day window — if a week's send
fails, the next one covers both weeks rather than dropping matches on the
floor.

Every attempt is recorded as a signup_email event (kind='digest' or
kind='confirm_reminder') so "did they get it?" stays answerable from the DB.
"""

from __future__ import annotations

import logging

from .config import settings
from .db import log_event, now, tx

log = logging.getLogger(__name__)

KIND = "digest"
REMINDER_KIND = "confirm_reminder"
# Lifetime cap on confirmation reminders per user — see the docstring.
REMINDER_MAX = 2
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


def unconfirmed_recipients(conn) -> list[dict]:
    """Signups that never proved their address. Shadow-ban and opt-out still
    exclude; bounce-dead addresses are filtered per-user in the send."""
    rows = conn.execute(
        "SELECT * FROM applicants"
        " WHERE email_confirmed_at IS NULL"
        "   AND COALESCE(shadow_banned, 0) = 0"
        "   AND COALESCE(digest_opt_out, 0) = 0"
        " ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def address_bounced(conn, email: str) -> bool:
    """True when this exact address has NDR'd — mailing it again is a bounce
    loop. A user who changed address after a bounce passes this check for the
    new (unproven but never-bounced) one, which is the right call."""
    return conn.execute(
        "SELECT 1 FROM events WHERE event_type = 'email:BOUNCED'"
        "   AND json_extract(payload_json,'$.to') = ? LIMIT 1",
        (email.lower(),)).fetchone() is not None


def reminders_ever_sent(conn, user_ref: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS c FROM events"
        " WHERE event_type = 'signup_email'"
        "   AND json_extract(payload_json,'$.kind') = ?"
        "   AND json_extract(payload_json,'$.user_ref') = ?"
        "   AND json_extract(payload_json,'$.ok') = 1",
        (REMINDER_KIND, user_ref)).fetchone()["c"]


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


def sent_recently(conn, user_ref: str, kind: str = KIND) -> bool:
    """Idempotence: a re-run of the weekly job must not mail twice."""
    return conn.execute(
        "SELECT 1 FROM events"
        " WHERE event_type = 'signup_email'"
        "   AND json_extract(payload_json,'$.kind') = ?"
        "   AND json_extract(payload_json,'$.user_ref') = ?"
        "   AND json_extract(payload_json,'$.ok') = 1"
        "   AND created_at >= datetime('now', ?) LIMIT 1",
        (kind, user_ref, f"-{RESEND_GUARD_DAYS} days")).fetchone() is not None


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


def _footer(edit_url: str) -> tuple[str, str]:
    html = ("<p style='color:#5B6B60;font-size:13px'>You get this once a "
            "week. Reply STOP to stop it.</p>")
    text = "\nYou get this once a week. Reply STOP to stop it."
    return html, text


def compose_near_only(user_ref: str, near: list[dict],
                      edit_url: str) -> tuple[str, str, str]:
    """No 7+ this week, but close ones exist: show them, and hand the user
    the dial. Positive framing — these are almost-there, not shortfalls."""
    base = (settings.dashboard_base_url or "").rstrip("/")
    page = f"{base}/job_matches/{user_ref}"
    n = len(near)
    subject = f"{n} close match{'es' if n != 1 else ''} this week — worth a look 🌱"
    items_html = "".join(
        f"<li><b>{x['score']}/10</b> — {x['company']} — {x['title']}</li>"
        for x in near)
    items_text = "\n".join(
        f"  {x['score']}/10  {x['company']} — {x['title']}" for x in near)
    fh, ft = _footer(edit_url)
    html = (
        f"<p>No job crossed the 7/10 line this week, but <b>{n}</b> came "
        f"close:</p><ul>{items_html}</ul>"
        f"<p>Close scores usually mean your profile and these jobs almost "
        f"speak the same language. Two dials worth turning: add a couple of "
        f"adjacent <b>job titles</b> (and synonyms) so we search wider, and "
        f"sharpen what you're looking for — "
        f"<a href='{edit_url}'>edit your profile here</a>.</p>"
        f"<p>Everything we've scored for you: <a href='{page}'>{page}</a></p>"
        f"{fh}")
    text = (
        f"No job crossed the 7/10 line this week, but {n} came close:\n\n"
        f"{items_text}\n\n"
        f"Add a couple of adjacent job titles (and synonyms) so we search "
        f"wider: {edit_url}\nAll your scored jobs: {page}\n{ft}")
    return subject, html, text


def compose_quiet_week(user_ref: str, titles: list[str],
                       edit_url: str) -> tuple[str, str, str]:
    """Nothing scored at all: say so honestly, show what we searched on, and
    make widening the search one tap away."""
    from . import notify
    base = (settings.dashboard_base_url or "").rstrip("/")
    page = f"{base}/job_matches/{user_ref}"
    subject = "A quiet week — let's widen the net 🌱"
    chips = notify.chips_html(titles) if titles else ""
    searched_html = (f"<p>This week we searched on:</p><p>{chips}</p>"
                     if titles else "")
    searched_text = (f"This week we searched on: {', '.join(titles)}\n"
                     if titles else "")
    fh, ft = _footer(edit_url)
    html = (
        f"<p>Nothing new stood out for you this week — that usually says "
        f"more about the search than about you.</p>"
        f"{searched_html}"
        f"<p>The fastest fix is breadth: add a few more <b>job titles</b> "
        f"(and the synonyms employers actually post), or widen your "
        f"locations — <a href='{edit_url}'>edit your profile here</a> and "
        f"next Monday's email draws from a bigger pond.</p>"
        f"<p>Your matches so far: <a href='{page}'>{page}</a></p>"
        f"{fh}")
    text = (
        f"Nothing new stood out for you this week — that usually says more "
        f"about the search than about you.\n{searched_text}"
        f"Add a few more job titles (and synonyms), or widen your "
        f"locations: {edit_url}\nYour matches so far: {page}\n{ft}")
    return subject, html, text


def compose_confirm_reminder(user_ref: str,
                             confirm_url: str) -> tuple[str, str, str]:
    subject = "One click and your job matches start 🌱"
    html = (
        f"<p>You signed up for jobpipe but never confirmed your email — so "
        f"nothing has been searched or matched for you yet. It all starts "
        f"the moment you confirm:</p>"
        f"<p><a href='{confirm_url}'><b>Confirm my email</b></a></p>"
        f"<p style='color:#5B6B60;font-size:13px'>If this wasn't you, "
        f"just ignore this. Reply STOP and we won't write again.</p>")
    text = (
        f"You signed up for jobpipe but never confirmed your email — "
        f"matching starts the moment you do: {confirm_url}\n"
        f"If this wasn't you, ignore this. Reply STOP and we won't write again.")
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

    from .profile_edit import ensure_edit_token, titles_from_row
    base = (settings.dashboard_base_url or "").rstrip("/")
    token = ensure_edit_token(conn, row["id"])
    edit_url = f"{base}/profile/{user_ref}/{token}"

    since = cutoff_for(conn, user_ref)
    matches = new_matches(conn, row["id"], since)
    near = near_misses(conn, row["id"], since)
    if matches:
        variant = "matches"
        subject, html, text = compose(user_ref, matches, near, edit_url)
    elif near:
        variant = "near_only"
        subject, html, text = compose_near_only(user_ref, near, edit_url)
    else:
        variant = "quiet_week"
        subject, html, text = compose_quiet_week(
            user_ref, titles_from_row(row), edit_url)
    # Quick-match-only profiles: show the titles we matched on (as chips, the
    # same visual language as the site) + the Full-match upgrade door.
    from . import notify
    from .profile_edit import is_quick_only, titles_from_row
    from .resume import get_resume
    no_resume = get_resume(conn, row["id"]) is None
    if is_quick_only(row):
        titles = titles_from_row(row)
        html += (f"<p style='margin-top:14px'>This week came from a <b>Quick "
                 f"match</b> on:</p><p>{notify.chips_html(titles)}</p>"
                 f"<p>Add skills, locations and deal-breakers for the "
                 f"<b>Full match</b> — and upload your resume for a "
                 f"candidate-fit view: <a href='{edit_url}'>complete your "
                 f"profile</a>.</p>")
        text += (f"\nThis week came from a Quick match on: "
                 f"{', '.join(titles)}.\nUpgrade to the Full match: "
                 f"{edit_url}\n")
    elif no_resume:
        html += (f"<p style='margin-top:14px'>P.S. Add your resume and every "
                 f"match also shows your <b>candidate fit</b>: "
                 f"<a href='{edit_url}'>your profile page</a>.</p>")
        text += f"\nP.S. Add your resume for candidate fit: {edit_url}\n"
    if dry_run:
        return (f"{user_ref}: WOULD send [{variant}] "
                f"{len(matches)} new / {len(near)} near to {email} "
                f"(since {since}) — subject: {subject!r}")

    from . import notify
    ok = notify.send_email(subject=subject, html_body=html, text_body=text,
                           to=email)
    with tx(conn):
        log_event(conn, "signup_email", payload={
            "kind": KIND, "user_ref": user_ref, "ok": ok, "to": email,
            "variant": variant, "n_new": len(matches), "n_near": len(near),
            "since": since})
    log.info("digest user=%s to=%s variant=%s new=%d near=%d sent=%s",
             user_ref, email, variant, len(matches), len(near), ok)
    return (f"{user_ref}: sent [{variant}] to {email}" if ok
            else f"{user_ref}: SEND FAILED to {email} — check SMTP logs")


def send_confirm_reminder(conn, row: dict, dry_run: bool = False) -> str:
    """Nudge a never-confirmed signup. Capped, bounce-aware, opt-out-aware."""
    user_ref = row["user_ref"] or ""
    try:
        from .profile import load_applicant_profile
        profile = load_applicant_profile(row)
        email = (getattr(profile.identity, "email", "") or "").strip()
    except Exception as e:  # noqa: BLE001 - report, don't crash the run
        return f"{user_ref}: could not load profile ({e})"
    if not email:
        return f"{user_ref}: no email on their profile — nothing to send"
    if address_bounced(conn, email):
        return f"{user_ref}: {email} has bounced — not mailing a dead address"
    if reminders_ever_sent(conn, user_ref) >= REMINDER_MAX:
        return f"{user_ref}: already reminded {REMINDER_MAX}x — leaving them be"
    if sent_recently(conn, user_ref, kind=REMINDER_KIND):
        return f"{user_ref}: reminded in the last {RESEND_GUARD_DAYS} days"

    from .confirm import ensure_confirm_token
    base = (settings.dashboard_base_url or "").rstrip("/")
    confirm_url = f"{base}/confirm/{user_ref}/{ensure_confirm_token(conn, row['id'])}"
    subject, html, text = compose_confirm_reminder(user_ref, confirm_url)
    if dry_run:
        return (f"{user_ref}: WOULD send [reminder] to {email} — "
                f"subject: {subject!r}")

    from . import notify
    ok = notify.send_email(subject=subject, html_body=html, text_body=text,
                           to=email)
    with tx(conn):
        log_event(conn, "signup_email", payload={
            "kind": REMINDER_KIND, "user_ref": user_ref, "ok": ok, "to": email})
    log.info("confirm reminder user=%s to=%s sent=%s", user_ref, email, ok)
    return (f"{user_ref}: sent [reminder] to {email}" if ok
            else f"{user_ref}: SEND FAILED to {email} — check SMTP logs")


def run(conn, dry_run: bool = False) -> dict:
    stats = {"considered": 0, "sent": 0, "skipped": 0, "failed": 0,
             "reminded": 0}
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
    for row in unconfirmed_recipients(conn):
        stats["considered"] += 1
        outcome = send_confirm_reminder(conn, row, dry_run=dry_run)
        if "SEND FAILED" in outcome:
            stats["failed"] += 1
        elif outcome.split(": ", 1)[-1].startswith(("sent", "WOULD send")):
            stats["reminded"] += 1
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
                      detail=f"sent={stats['sent']} reminded={stats['reminded']} "
                             f"skipped={stats['skipped']} failed={stats['failed']}")
        log.info("digest complete: %s at %s", stats, now())
    except Exception as e:
        if not dry_run:
            heartbeat(conn, "digest", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
