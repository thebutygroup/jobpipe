"""Signup email confirmation (double opt-in).

A signup is inert until the address behind it is proven: the applicant row is
created with active = 0 and email_confirmed_at NULL, and nothing in the
pipeline will score a posting for them until they click the link we mail.

Why the gate is worth the friction: matching costs model calls per user per
posting, and mail to an address nobody proved is how a sender's reputation
gets spent. An unconfirmed signup should cost us nothing.

Existing users are grandfathered by the migration in db.py — they signed up
before this rule existed and were never asked to confirm.
"""

from __future__ import annotations

import hmac
import secrets

from .db import now


def ensure_confirm_token(conn, applicant_id: int) -> str:
    """Issue (or reuse) the secret that appears in the confirmation URL."""
    row = conn.execute("SELECT confirm_token FROM applicants WHERE id = ?",
                       (applicant_id,)).fetchone()
    if row and row["confirm_token"]:
        return row["confirm_token"]
    token = secrets.token_urlsafe(16)
    conn.execute("UPDATE applicants SET confirm_token = ? WHERE id = ?",
                 (token, applicant_id))
    conn.commit()
    return token


def confirm(conn, user_ref: str, token: str) -> dict | None:
    """Redeem a confirmation token. Returns the applicant row, or None.

    Returns the row for an ALREADY-confirmed user too, so that a second click
    on the same link (mail clients prefetch; people re-click) reads as success
    rather than an alarming failure. The token is compared in constant time —
    it is a bearer secret.
    """
    if not (user_ref and token):
        return None
    row = conn.execute("SELECT * FROM applicants WHERE user_ref = ?",
                       (user_ref,)).fetchone()
    if row is None or not row["confirm_token"]:
        return None
    if not hmac.compare_digest(str(row["confirm_token"]), str(token)):
        return None
    if not row["email_confirmed_at"]:
        conn.execute("UPDATE applicants SET email_confirmed_at = ? WHERE id = ?",
                     (now(), row["id"]))
        conn.commit()
        row = conn.execute("SELECT * FROM applicants WHERE id = ?",
                           (row["id"],)).fetchone()
    return dict(row)


def is_confirmed(conn, user_ref: str) -> bool:
    row = conn.execute("SELECT email_confirmed_at FROM applicants WHERE user_ref = ?",
                       (user_ref,)).fetchone()
    return bool(row and row["email_confirmed_at"])
