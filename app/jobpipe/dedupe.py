"""Cross-source job identity resolution.

The same job surfaces on multiple sources (its ATS board, Built In, Adzuna,
Reed) with different URLs, external IDs and cosmetic title/company variants.
This module decides whether an incoming posting IS an existing canonical
posting, so ingestion attaches provenance to one Job instead of minting
duplicates — which is the foundation for the source analytics (uniqueness,
overlap, freshness per source).

Matching heuristic — deliberately explainable, in order:
  1. exact identity_key (canonical apply URL) — handled in db.upsert_posting
  2. same normalised company AND same normalised title AND compatible location
  3. same normalised company AND fuzzy title (rapidfuzz token_sort_ratio >=
     TITLE_FUZZ_THRESHOLD after strip/lower/de-punctuate) AND compatible location

Every link records an explanation payload in the events table
(event_type='dedupe_linked'), so any merge can be audited later.

Canonical preference: ATS/direct postings outrank board scrapes, which
outrank aggregators. When a higher-ranked source arrives for an existing
lower-ranked canonical row, the row is promoted in place (URL/identity/source
updated) — provenance history is untouched.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .models import PostingDTO, normalise_title

TITLE_FUZZ_THRESHOLD = 90.0

# Lower rank = preferred canonical record.
SOURCE_RANK = {"ats": 0, "manual": 0, "builtin": 1}
DEFAULT_RANK = 2  # aggregators


def source_rank(source: str) -> int:
    return SOURCE_RANK.get(source, DEFAULT_RANK)


_LEGAL_SUFFIXES = re.compile(
    r"\b(ltd|limited|plc|inc|incorporated|llc|llp|gmbh|s\.?a\.?|co|corp|"
    r"corporation|group|holdings|uk)\b\.?", re.I)
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def normalise_company(name: str) -> str:
    """'The Acme Group Ltd.' -> 'acme' ; 'Acme, Inc' -> 'acme'."""
    s = (name or "").lower()
    s = _PUNCT.sub(" ", s)
    s = _LEGAL_SUFFIXES.sub(" ", s)
    s = re.sub(r"^the\s+", "", s.strip())
    return " ".join(s.split())


def locations_compatible(a: str, b: str) -> bool:
    """City-level compatibility: 'London' vs 'London, UK' -> True.
    An empty side is compatible with anything (many boards omit location)."""
    ta = _location_tokens(a)
    tb = _location_tokens(b)
    if not ta or not tb:
        return True
    return bool(ta & tb)


_LOC_NOISE = {"uk", "united", "kingdom", "england", "greater", "central", "hybrid",
              "remote", "area", "city", "of"}


def _location_tokens(loc: str) -> set[str]:
    tokens = set(_PUNCT.sub(" ", (loc or "").lower()).split())
    return tokens - _LOC_NOISE


def titles_match(a: str, b: str) -> tuple[bool, str, float]:
    """(match?, rule, score). Exact after normalisation, else fuzzy."""
    na, nb = normalise_title(a), normalise_title(b)
    if not na or not nb:
        return False, "empty", 0.0
    if na == nb:
        return True, "title_exact", 100.0
    score = fuzz.token_sort_ratio(na, nb)
    if score >= TITLE_FUZZ_THRESHOLD:
        return True, "title_fuzzy", float(score)
    return False, "title_mismatch", float(score)


def find_canonical(conn, dto: PostingDTO) -> tuple[int | None, dict]:
    """Find the existing canonical posting this DTO duplicates, if any.

    Returns (posting_id | None, explanation). The explanation is stored on
    the dedupe_linked event so every merge is auditable.
    """
    company_norm = normalise_company(dto.company_name)
    if not company_norm:
        return None, {"rule": "no_company"}
    # Narrow in SQL by the company's first significant token, verify in Python.
    first_token = company_norm.split()[0]
    candidates = conn.execute(
        "SELECT p.id, p.title, p.location, p.source, c.name AS company_name "
        "FROM postings p JOIN companies c ON c.id = p.company_id "
        "WHERE p.duplicate_of IS NULL AND lower(c.name) LIKE ?",
        (f"%{first_token}%",)).fetchall()
    best: tuple[float, int, dict] | None = None
    for row in candidates:
        if normalise_company(row["company_name"]) != company_norm:
            continue
        ok, rule, score = titles_match(dto.title, row["title"])
        if not ok:
            continue
        if not locations_compatible(dto.location, row["location"]):
            continue
        explanation = {
            "rule": rule, "title_score": score,
            "company_norm": company_norm,
            "matched_posting": row["id"], "matched_title": row["title"],
            "incoming_source": dto.source_detail or dto.source,
            "canonical_source": row["source"],
        }
        if best is None or score > best[0]:
            best = (score, row["id"], explanation)
    if best is None:
        return None, {"rule": "no_match"}
    return best[1], best[2]
