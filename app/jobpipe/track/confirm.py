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
        if not (settings.smtp_user and settings.smtp_password):
            log.warning("mail creds absent; skipping confirmation tracking")
            return
        msgs = fetch_recent(settings.imap_host, settings.imap_port,
                            settings.smtp_user, settings.smtp_password)
        n = match_and_confirm(conn, msgs)
        o = process_outcomes(conn, msgs)
        heartbeat(conn, "track", ok=True, detail=f"confirmed={n} outcomes={o}")
        log.info("confirmation tracking: %d confirmed, %d outcomes", n, o)
    except Exception as e:
        heartbeat(conn, "track", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
