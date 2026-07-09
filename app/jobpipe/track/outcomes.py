"""Classify forwarded emails into application outcomes and link them.

Joe forwards any rejection / interview / assessment email to the service
inbox. The hourly tracker picks them up alongside ATS confirmations:

  classify: deterministic keyword tiers first (temp-0-style precision);
            genuinely ambiguous emails fall back to one Haiku call when an
            API key is configured, else 'other'
  link:     company names from SUBMITTED/CONFIRMED applications are searched
            in the subject + body; exactly one hit -> linked automatically,
            otherwise the outcome is stored unlinked with a company guess
            for manual linking on the dashboard

Every outcome row keeps the subject/from/snippet so the analytics layer can
be re-derived or audited later.
"""
from __future__ import annotations

import json
import logging
import re

from ..config import settings
from ..db import log_event, now, tx

log = logging.getLogger(__name__)

# Deterministic tiers: (outcome_type, patterns). First match wins; order matters:
# interview signals beat rejection boilerplate that sometimes shares an email.
_TIERS: list[tuple[str, list[str]]] = [
    ("interview_invite", [
        r"\binterview\b.{0,40}\b(schedul|invit|arrang|book|confirm)",
        r"\b(schedul|invit|arrang|book)\w*\b.{0,40}\binterview\b",
        r"\bspeak with\b.{0,40}\bteam\b",
        r"\bnext (step|stage)\b.{0,60}\b(call|interview|chat|conversation)\b",
        r"\bphone (screen|call)\b",
    ]),
    ("assessment", [
        r"\b(coding|technical|take.?home|online) (test|assessment|challenge|exercise)\b",
        r"\bhackerrank|codility|codesignal\b",
    ]),
    ("offer", [
        r"\b(pleased|delighted|happy) to (offer|extend)\b",
        r"\boffer of employment\b",
    ]),
    ("rejected", [
        r"\bunfortunately\b",
        r"\bnot (be )?(moving|progressing|proceeding) (forward|ahead)\b",
        r"\bother candidates?\b",
        r"\bnot to (move|progress|proceed)\b",
        r"\bposition has been filled\b",
        r"\bwe (will not|won't) be (progressing|taking)\b",
        r"\bdecided to pursue\b",
    ]),
    ("withdrawn", [
        r"\b(withdraw|withdrawn|cancelled) (your |the )?application\b",
    ]),
]

LLM_CLASSIFY_PROMPT = """Classify this job-application email for the candidate.
Respond ONLY with a JSON object, no fences, no prose:
{{"outcome": "<rejected|interview_invite|assessment|offer|withdrawn|other>",
 "company": "<company name if identifiable, else empty>"}}

SUBJECT: {subject}

BODY:
{body}
"""


def classify(subject: str, body: str, client=None) -> tuple[str, str]:
    """Return (outcome_type, company_guess). Deterministic first, LLM fallback."""
    text = f"{subject}\n{body}".lower()
    for outcome, patterns in _TIERS:
        for pat in patterns:
            if re.search(pat, text):
                return outcome, ""
    if client is not None:
        try:
            resp = client.messages.create(
                model=settings.match_model, max_tokens=200, temperature=0,
                messages=[{"role": "user", "content": LLM_CLASSIFY_PROMPT.format(
                    subject=subject[:200], body=body[:6000])}])
            data = json.loads(resp.content[0].text.strip())
            if data.get("outcome") in {t for t, _ in _TIERS} | {"other"}:
                return data["outcome"], data.get("company", "")
        except Exception:
            log.warning("LLM outcome classification failed", exc_info=True)
    return "other", ""


def link_application(conn, subject: str, body: str) -> tuple[int | None, str]:
    """Match email text against companies of submitted/confirmed apps.
    Exactly one company hit -> that application (most recent). Else unlinked."""
    text = f"{subject}\n{body}".lower()
    rows = conn.execute(
        "SELECT a.id, c.name FROM applications a "
        "JOIN postings p ON p.id = a.posting_id "
        "JOIN companies c ON c.id = p.company_id "
        "WHERE a.state IN ('SUBMITTED','CONFIRMED') "
        "ORDER BY a.updated_at DESC").fetchall()
    hits: dict[str, int] = {}
    for r in rows:
        name = r["name"].lower().removesuffix(" inc.").removesuffix(" ltd")
        if len(name) >= 3 and name in text and name not in hits:
            hits[name] = r["id"]
    if len(hits) == 1:
        name, app_id = next(iter(hits.items()))
        return app_id, name
    return None, "/".join(sorted(hits)) if hits else ""


def record_outcome(conn, subject: str, sender: str, body: str, client=None) -> dict:
    outcome, llm_company = classify(subject, body, client)
    app_id, company_guess = link_application(conn, subject, body)
    with tx(conn):
        conn.execute(
            "INSERT INTO outcomes (application_id, outcome_type, occurred_at, source,"
            " email_subject, email_from, snippet, company_guess, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (app_id, outcome, now(), "email", subject[:300], sender[:200],
             body[:500], company_guess or llm_company, now()))
        log_event(conn, f"outcome:{outcome}", application_id=app_id,
                  payload={"linked": app_id is not None, "subject": subject[:120]})
    return {"outcome": outcome, "application_id": app_id}
