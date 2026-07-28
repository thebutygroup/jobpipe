"""Confirmation tracking: match 'application received' emails to SUBMITTED apps.

IMAP poll over the Zoho mailbox (same account as SMTP). Matches by company
token + recency, transitions SUBMITTED -> CONFIRMED. Newsletters and unrelated
mail are ignored by requiring an application-intent phrase.
"""

from __future__ import annotations

import email
import imaplib
import logging
import re
from email.header import decode_header

from ..config import settings
from ..db import connect, heartbeat, transition, tx

log = logging.getLogger(__name__)
INTENT = re.compile(r"application (received|submitted)|thank you for applying|"
                    r"we('| ha)ve received your application|your application", re.I)


def _decode(value: str) -> str:
    parts = decode_header(value or "")
    return "".join(p.decode(enc or "utf-8", "ignore") if isinstance(p, bytes) else p
                   for p, enc in parts)


def submitted_apps(conn) -> list:
    return conn.execute(
        "SELECT a.id, c.name AS company, a.submitted_at FROM applications a "
        "JOIN postings p ON p.id = a.posting_id JOIN companies c ON c.id = p.company_id "
        "WHERE a.state = 'SUBMITTED'").fetchall()


def _body_text(msg) -> str:
    """Best-effort text/plain body, decoded, capped."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() == "text/plain":
            try:
                payload = part.get_payload(decode=True) or b""
                return payload.decode(part.get_content_charset() or "utf-8",
                                      errors="replace")[:8000]
            except Exception:
                continue
    return ""


def fetch_recent(host, port, user, password, since_days=7):
    M = imaplib.IMAP4_SSL(host, port)
    try:
        M.login(user, password)
        M.select("INBOX")
        import datetime as dt

        since = (dt.date.today() - dt.timedelta(days=since_days)).strftime("%d-%b-%Y")
        _typ, data = M.search(None, f'(SINCE {since})')
        out = []
        for num in data[0].split():
            _typ, msg_data = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            out.append((_decode(msg.get("From", "")), _decode(msg.get("Subject", "")),
                        msg.get("Message-ID", ""), _body_text(msg)))
        return out
    finally:
        try:
            M.close()
        except Exception:
            pass
        M.logout()


def match_and_confirm(conn, messages) -> int:
    apps = submitted_apps(conn)
    confirmed = 0
    for sender, subject, msg_id, _body in messages:
        blob = f"{sender} {subject}"
        if not INTENT.search(blob):
            continue
        for app in apps:
            token = app["company"].split()[0].lower()
            if token in blob.lower():
                with tx(conn):
                    conn.execute("UPDATE applications SET confirmation_msg_id = ?"
                                 " WHERE id = ?", (msg_id, app["id"]))
                transition(conn, app["id"], "CONFIRMED", payload={"msg_id": msg_id})
                confirmed += 1
                break
    return confirmed


FWD = re.compile(r"^\s*(fwd?|fw)\s*:", re.I)


def process_opt_outs(conn, messages) -> int:
    """Honour "reply STOP" from the weekly digest.

    The digest tells users they can reply STOP; if nothing read the replies
    that would be a promise we don't keep, which is worse than having no
    digest at all. Deliberately strict — the reply must be essentially just
    the word, so a posting titled "STOP the press" quoted in a thread can't
    silently unsubscribe someone.

    Matching is by sender address against the address on each profile, so a
    STOP from an unrelated mailbox cannot opt out somebody else.
    """
    from ..profile import load_applicant_profile

    senders = set()
    for sender, subject, _msg_id, body in messages:
        first = ((body or "").strip().splitlines() or [""])[0].strip()
        candidate = first or (subject or "").strip()
        if re.fullmatch(r"(?i)\s*(stop|unsubscribe)\s*[.!]?", candidate or ""):
            addr = email.utils.parseaddr(sender or "")[1].lower()
            if addr:
                senders.add(addr)
    if not senders:
        return 0

    stopped = 0
    rows = conn.execute(
        "SELECT * FROM applicants WHERE COALESCE(digest_opt_out, 0) = 0").fetchall()
    for row in rows:
        try:
            profile = load_applicant_profile(row)
            addr = (getattr(profile.identity, "email", "") or "").strip().lower()
        except Exception:
            continue
        if addr and addr in senders:
            with tx(conn):
                conn.execute("UPDATE applicants SET digest_opt_out = 1 WHERE id = ?",
                             (row["id"],))
            log.info("digest opt-out: %s replied STOP", row["user_ref"])
            stopped += 1
    return stopped


def process_outcomes(conn, messages) -> int:
    """Forwarded emails (Fwd:/FW: subjects) become outcome records."""
    from .outcomes import record_outcome
    client = None
    if settings.anthropic_api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    recorded = 0
    for sender, subject, msg_id, body in messages:
        if not FWD.match(subject or ""):
            continue
        if conn.execute("SELECT 1 FROM outcomes WHERE email_msg_id = ?",
                        (msg_id,)).fetchone():
            continue
        r = record_outcome(conn, subject, sender, body, client)
        with tx(conn):
            conn.execute("UPDATE outcomes SET email_msg_id = ? WHERE id = "
                         "(SELECT MAX(id) FROM outcomes)", (msg_id,))
        log.info("outcome recorded: %s (app=%s)", r["outcome"], r["application_id"])
        recorded += 1
    return recorded


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        if not settings.track_enabled:
            log.info("outcome tracking disabled (TRACK_ENABLED=false); skipping")
            return
        imap_user = settings.imap_user or settings.smtp_user
        imap_password = settings.imap_password or settings.smtp_password
        if not (imap_user and imap_password):
            log.warning("mail creds absent; skipping confirmation tracking")
            return
        msgs = fetch_recent(settings.imap_host, settings.imap_port,
                            imap_user, imap_password)
        n = match_and_confirm(conn, msgs)
        o = process_outcomes(conn, msgs)
        s = process_opt_outs(conn, msgs)
        heartbeat(conn, "track", ok=True,
                  detail=f"confirmed={n} outcomes={o} opt_outs={s}")
        log.info("confirmation tracking: %d confirmed, %d outcomes, %d opt-outs",
                 n, o, s)
    except Exception as e:
        heartbeat(conn, "track", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
