"""Untrusted-text safety for self-serve profile editing.

Users edit their profile through STRUCTURED TEXT BOXES with hard character
limits — never raw YAML (the server rebuilds and validates the YAML itself).
Every submitted field is screened for prompt-injection patterns before it can
reach a model prompt. A flagged submission:

  1. emails the owner with the user_ref and the offending text,
  2. shadow-bans the applicant (pipeline silently stops matching/searching/
     emailing for them; their pages still render so nothing looks different),
  3. tells the user "saved" — the shadow ban is invisible by design.

Screening is heuristic scoring, not a model call: strong patterns flag alone,
weak ones need two hits. Tuned to keep normal career prose (including "I want
a 10/10 role!") off the tripwire.
"""

from __future__ import annotations

import logging
import re

from .db import log_event, tx

log = logging.getLogger(__name__)

# hard character limits per form field (server-enforced; the form mirrors them)
FIELD_LIMITS = {
    "email": 120,
    "target_titles": 200,
    "title_synonyms": 200,
    "positioning": 600,
    "experience": 2000,
    "skills": 600,
    "locations_ok": 200,
    "hard_nos": 200,
    "salary_min": 12,
}

# Patterns that are near-certain injection attempts (one hit flags).
STRONG = [
    r"ignore\s+(all|any|previous|prior|above|earlier)\s+(instructions?|prompts?|rules?)",
    r"disregard\s+(all|any|previous|prior|the)\s+(instructions?|prompts?|rules?)",
    r"(system|assistant|developer)\s+prompt",
    r"you\s+are\s+(now|no\s+longer)\s",
    r"new\s+instructions?\s*:",
    r"</?\s*(system|assistant|instructions?|prompt)\s*>",
    r"<<<|>>>",                      # our own prompt fence markers
    r"score\s+(me|this|every|all)\b.{0,40}\b(10|ten|highest|maximum)",
    r"(always|must)\s+(score|rate|rank)\b.{0,30}\b(10|ten|high)",
    r"do\s+anything\s+now|\bDAN\b\s+mode|jailbreak",
    r"print\s+(your|the)\s+(instructions?|prompt|system)",
]
# Weaker signals (two distinct hits flag).
WEAK = [
    r"\bact\s+as\b",
    r"\bpretend\s+(to\s+be|you)\b",
    r"respond\s+only\s+with",
    r"\boverride\b",
    r"\bAI\s+model\b|\blanguage\s+model\b",
    r"\{\{.*?\}\}|\$\{.*?\}",
    r"base64|\\x[0-9a-f]{2}",
]

_STRONG = [re.compile(p, re.IGNORECASE) for p in STRONG]
_WEAK = [re.compile(p, re.IGNORECASE) for p in WEAK]


def looks_like_injection(text: str) -> tuple[bool, str]:
    """(flagged, reason). Screens ONE text blob."""
    t = text or ""
    for rx in _STRONG:
        m = rx.search(t)
        if m:
            return True, f"strong pattern: {m.group(0)[:60]!r}"
    weak_hits = [rx.pattern for rx in _WEAK if rx.search(t)]
    if len(weak_hits) >= 2:
        return True, f"multiple weak patterns: {weak_hits[:3]}"
    return False, ""


def screen_fields(fields: dict[str, str]) -> tuple[bool, str]:
    """Screen every submitted field; also enforce limits defensively (the
    form enforces them client-side, but the server is the authority)."""
    for name, value in fields.items():
        limit = FIELD_LIMITS.get(name)
        if limit and len(value or "") > limit:
            return True, f"field {name} exceeds limit ({len(value)} > {limit})"
        flagged, reason = looks_like_injection(value or "")
        if flagged:
            return True, f"field {name}: {reason}"
    return False, ""


def shadow_ban(conn, applicant_id: int, user_ref: str, reason: str,
               offending_text: str = "") -> None:
    """Flip the ban, record it, email the owner. Never raises."""
    try:
        with tx(conn):
            conn.execute("UPDATE applicants SET shadow_banned = 1 WHERE id = ?",
                         (applicant_id,))
            log_event(conn, "shadow_banned", payload={
                "applicant_id": applicant_id, "user_ref": user_ref,
                "reason": reason})
    except Exception:
        log.exception("could not record shadow ban for %s", user_ref)
    try:
        from . import notify
        snippet = (offending_text or "")[:800].replace("<", "&lt;")
        notify.send_email(
            subject=f"[jobpipe] SHADOW BANNED: {user_ref} — possible prompt injection",
            html_body=(
                f"<p><b>{user_ref}</b> (applicant {applicant_id}) submitted "
                f"profile text that tripped the injection screen and has been "
                f"shadow-banned: matching, searches and emails silently stop "
                f"for them; their pages still render.</p>"
                f"<p>Reason: {reason}</p>"
                f"<pre style='white-space:pre-wrap'>{snippet}</pre>"
                f"<p>False positive? Unban with:<br>"
                f"<code>UPDATE applicants SET shadow_banned=0 WHERE id={applicant_id}</code></p>"),
            text_body=f"{user_ref} shadow-banned: {reason}\n\n{offending_text[:800]}")
    except Exception:
        log.exception("could not email shadow-ban alert for %s", user_ref)


def is_shadow_banned(conn, applicant_id: int) -> bool:
    row = conn.execute("SELECT shadow_banned FROM applicants WHERE id = ?",
                       (applicant_id,)).fetchone()
    return bool(row and row["shadow_banned"])
