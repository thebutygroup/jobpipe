"""Shared DTOs and constants."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# ---- application states ----------------------------------------------------------------
DISCOVERED = "DISCOVERED"
PREFILTERED = "PREFILTERED"
MATCHED = "MATCHED"
REJECTED_AUTO = "REJECTED_AUTO"
PREPARED = "PREPARED"
PENDING_REVIEW = "PENDING_REVIEW"
APPROVED = "APPROVED"
REJECTED_HUMAN = "REJECTED_HUMAN"
SUBMITTING = "SUBMITTING"
SUBMITTED = "SUBMITTED"
CONFIRMED = "CONFIRMED"
NEEDS_HUMAN = "NEEDS_HUMAN"
FAILED = "FAILED"

LEGAL_TRANSITIONS: dict[str, set[str]] = {
    DISCOVERED: {PREFILTERED, REJECTED_AUTO},
    PREFILTERED: {MATCHED, REJECTED_AUTO},
    MATCHED: {PREPARED, NEEDS_HUMAN, FAILED, SUBMITTED},      # SUBMITTED = applied manually
    PREPARED: {PENDING_REVIEW, SUBMITTED},
    PENDING_REVIEW: {APPROVED, REJECTED_HUMAN, SUBMITTED},
    APPROVED: {SUBMITTING},
    SUBMITTING: {SUBMITTED, NEEDS_HUMAN, FAILED},
    SUBMITTED: {CONFIRMED},
    NEEDS_HUMAN: {SUBMITTING, PENDING_REVIEW, FAILED, SUBMITTED},   # resume after human fix
    FAILED: {SUBMITTING, MATCHED},                        # retry paths
    REJECTED_AUTO: set(),
    REJECTED_HUMAN: set(),
    CONFIRMED: set(),
}

TRACKING_PARAM_PREFIXES = ("utm_", "gh_", "lever-", "ashby_")
TRACKING_PARAMS = {"source", "ref", "src", "gclid", "fbclid"}


def canonicalise_url(url: str) -> str:
    """Normalise an apply URL into a canonical identity: lowercase host,
    strip tracking params and fragments, drop trailing slash."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not (k.lower() in TRACKING_PARAMS or k.lower().startswith(TRACKING_PARAM_PREFIXES))
    ]
    path = parts.path.rstrip("/").lower()  # identity key only, never fetched
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


def content_hash(*fields: str) -> str:
    h = hashlib.sha256()
    for f in fields:
        h.update((f or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


@dataclass
class PostingDTO:
    company_name: str
    source: str  # 'ats' | 'builtin' | 'manual'
    external_id: str
    title: str
    location: str = ""
    remote_policy: str = ""
    department: str = ""
    apply_url: str = ""
    description_text: str = ""
    raw: dict = field(default_factory=dict)
    # Provenance tag: the specific adapter that saw this posting (greenhouse,
    # lever, adzuna, ...). `source` stays the coarse channel ('ats', 'builtin',
    # 'adzuna', ...) used by postings.source; source_detail feeds source_postings.
    source_detail: str = ""

    @property
    def canonical_apply_url(self) -> str:
        return canonicalise_url(self.apply_url)

    @property
    def hash(self) -> str:
        return content_hash(self.title, self.location, self.description_text)
