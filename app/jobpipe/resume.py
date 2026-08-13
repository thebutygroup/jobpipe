"""Resume intake: PDF in, plain text kept, PDF discarded.

Design (claude/bidirectional-match-design.md, R1): we never store the
uploaded file — pypdf extracts the text at upload time, whitespace is
normalised, and only the text (plus sha/size of the upload, for the
display card and replace-detection) is written to the resumes table.

The text is a NEW CHANNEL into the matcher prompt, so it goes through the
same injection screen as profile fields. Flagged text is stored with
text_flagged=1 and must never reach a prompt — prompt_text_for() is the
single accessor that enforces this.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re

from .db import log_event, now, tx

log = logging.getLogger(__name__)

MAX_SIZE_BYTES = 5 * 1024 * 1024          # 5 MB (Joe, 13 Aug)
MIN_TEXT_CHARS = 200                      # below this: scanned/image-only PDF
MAX_TEXT_CHARS = 40_000                   # sanity cap on stored text


# Rejections are passed around as CODES and mapped to fixed copy at render
# time — no user-controllable text is ever reflected into the page.
ERRORS = {
    "nofile": "no file received — pick your resume PDF and try again",
    "toobig": "that file is over 5 MB — export a smaller PDF",
    "notpdf": "that doesn't look like a PDF — please upload a PDF file",
    "noread": "we couldn't read that PDF — try re-exporting it",
    "notext": "this PDF has no selectable text (it may be a scan) — export "
              "a text-based PDF from your editor instead",
}


class ResumeError(Exception):
    """User-visible rejection. .code is one of ERRORS' keys."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(ERRORS.get(code, code))


def extract_text(data: bytes) -> str:
    """PDF bytes -> normalised plain text. Raises ResumeError for anything
    a user can fix (wrong type, too big, no selectable text)."""
    if not data:
        raise ResumeError("nofile")
    if len(data) > MAX_SIZE_BYTES:
        raise ResumeError("toobig")
    if not data.startswith(b"%PDF-"):
        # magic bytes, never the filename: a .pdf extension proves nothing
        raise ResumeError("notpdf")
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
    except Exception:
        log.exception("pypdf could not read an uploaded resume")
        raise ResumeError("noread")
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    # normalise: collapse runs of blank lines and intra-line whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) < MIN_TEXT_CHARS:
        raise ResumeError("notext")
    return text[:MAX_TEXT_CHARS]


def save_resume(conn, applicant_id: int, user_ref: str, filename: str,
                data: bytes) -> dict:
    """Validate + extract + store (replacing any existing 'default' resume).
    Returns the stored row as a dict. Raises ResumeError on rejection."""
    text = extract_text(data)

    from . import safety
    flagged, reason = safety.looks_like_injection(text)
    if flagged:
        log.warning("resume text flagged for %s: %s", user_ref, reason)

    sha = hashlib.sha256(data).hexdigest()
    existing = conn.execute(
        "SELECT id FROM resumes WHERE applicant_id = ? AND label = 'default'",
        (applicant_id,)).fetchone()
    with tx(conn):
        if existing:
            conn.execute(
                "UPDATE resumes SET filename = ?, upload_sha256 = ?,"
                " upload_size_bytes = ?, resume_text = ?, text_flagged = ?,"
                " updated_at = ? WHERE id = ?",
                (filename[:200], sha, len(data), text, int(flagged), now(),
                 existing["id"]))
        else:
            conn.execute(
                "INSERT INTO resumes (applicant_id, label, filename,"
                " upload_sha256, upload_size_bytes, resume_text, text_flagged,"
                " uploaded_at, updated_at) VALUES (?,'default',?,?,?,?,?,?,?)",
                (applicant_id, filename[:200], sha, len(data), text,
                 int(flagged), now(), now()))
        log_event(conn, "resume:REPLACED" if existing else "resume:UPLOADED",
                  payload={"applicant_id": applicant_id, "user_ref": user_ref,
                           "sha256": sha, "size": len(data),
                           "flagged": bool(flagged)})
    return get_resume(conn, applicant_id)


def get_resume(conn, applicant_id: int) -> dict | None:
    """The applicant's current resume row (display metadata + text)."""
    row = conn.execute(
        "SELECT * FROM resumes WHERE applicant_id = ? AND label = 'default'",
        (applicant_id,)).fetchone()
    return dict(row) if row else None


def prompt_text_for(conn, applicant_id: int) -> str:
    """The ONLY way matcher code should read resume text: flagged text
    (possible prompt injection) is never returned."""
    row = get_resume(conn, applicant_id)
    if not row or row["text_flagged"]:
        return ""
    return row["resume_text"]
