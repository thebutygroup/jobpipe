"""Derive polling searches from active applicant profiles.

searches.yaml is the owner's hand-written list — shaped around the owner's
own titles. The moment a second person signs up wanting something else
("Head of Data", "Photographer", "Business Development"), the pipeline is
scoring them against the wrong pond: nothing it fetches is relevant to them.

This module closes that gap: every ACTIVE applicant's target_titles (and
synonyms) become extra searches on the sources that accept arbitrary queries —
adzuna and reed (API keyword searches) and builtin (constructed saved-search
URL). Hand-written YAML entries always win: dedupe is case-insensitive on
(source, keywords), so nothing is fetched twice.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, quote_plus, urlsplit

from .config import settings
from .profile import ProfileError, load_applicant_profile

log = logging.getLogger(__name__)

QUERY_SOURCES = ("builtin", "adzuna", "reed")   # sources that take arbitrary terms
BUILTIN_SEARCH_URL = "https://builtinlondon.uk/jobs?search={q}"


def _norm(kw: str) -> str:
    return " ".join((kw or "").lower().split())


def _slug(kw: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", _norm(kw)).strip("-")[:40]


def _builtin_url_keywords(url: str) -> str:
    """Extract the search term from a Built In saved-search URL for dedupe."""
    try:
        q = parse_qs(urlsplit(url).query).get("search", [""])[0]
    except ValueError:
        q = ""
    return _norm(q.replace("+", " "))


def _existing_keys(existing: list[dict]) -> set[tuple[str, str]]:
    keys = set()
    for s in existing:
        source = s.get("source") or ("builtin" if s.get("url") else "")
        kw = _norm(s.get("keywords", "")) or _builtin_url_keywords(s.get("url", ""))
        if source and kw:
            keys.add((source, kw))
    return keys


def derive_profile_searches(conn, existing: list[dict],
                            cap: int | None = None) -> list[dict]:
    """Extra searches.yaml-shaped entries derived from active applicants.
    Deduped against `existing` and each other; capped to protect API budgets
    (anything dropped by the cap is logged, never silently)."""
    cap = settings.profile_searches_cap if cap is None else cap
    disabled = disabled_sources()
    seen = _existing_keys(existing)
    derived: list[dict] = []
    dropped = 0
    rows = conn.execute(
        "SELECT * FROM applicants WHERE active = 1 ORDER BY id").fetchall()
    for row in rows:
        try:
            profile = load_applicant_profile(row)
        except (ProfileError, OSError) as e:
            log.warning("profile searches: skipping %s (%s)", row["user_ref"], e)
            continue
        titles = [t for t in (profile.preferences.target_titles
                              + profile.preferences.title_synonyms) if t.strip()]
        locations = [loc for loc in profile.preferences.locations_ok
                     if loc.strip() and "remote" not in loc.lower()]
        location = (locations[0].split("(")[0].strip() if locations else "London")
        who = row["user_ref"] or f"applicant{row['id']}"
        for title in titles:
            kw = _norm(title)
            if not kw:
                continue
            for source in QUERY_SOURCES:
                if source in disabled or (source, kw) in seen:
                    continue
                seen.add((source, kw))
                if len(derived) >= cap:
                    dropped += 1
                    continue
                entry = {"name": f"profile-{who}-{_slug(kw)}-{source}",
                         "source": source, "keywords": kw, "location": location}
                if source == "builtin":
                    entry["url"] = BUILTIN_SEARCH_URL.format(q=quote_plus(kw))
                derived.append(entry)
    if dropped:
        log.warning("profile searches: cap %d reached — %d search(es) dropped "
                    "(raise PROFILE_SEARCHES_CAP to cover them)", cap, dropped)
    return derived


def disabled_sources() -> set[str]:
    """Sources the owner has switched off (DISABLED_SOURCES=adzuna,... in
    .env). Keys stay in place; the source just stops being polled."""
    return {s.strip().lower() for s in settings.disabled_sources.split(",")
            if s.strip()}
