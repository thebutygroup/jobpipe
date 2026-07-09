"""Deterministic pre-filter: cut obviously irrelevant postings before any LLM spend.

Pass = title matches a target title/synonym AND location passes
(locations_ok match, or empty location = benefit of the doubt) AND no hard-no hit.
"""

from __future__ import annotations

import logging
import re

from ..config import settings
from ..db import connect, heartbeat, log_event, tx
from ..models import PREFILTERED, REJECTED_AUTO, normalise_title
from ..profile import Profile, load_profile

log = logging.getLogger(__name__)

DEFAULT_SYNONYMS = [
    "fde", "forward deployed", "forward-deployed",
    "solutions engineer ai", "member of technical staff", "ai engineer",
]


def title_matches(title: str, profile: Profile) -> bool:
    norm = normalise_title(title)
    candidates = [normalise_title(t) for t in
                  profile.preferences.target_titles + profile.preferences.title_synonyms
                  + DEFAULT_SYNONYMS]
    return any(c and c in norm for c in candidates)


def location_ok(location: str, profile: Profile) -> bool:
    if not location.strip():
        return True  # unknown location: keep, let the matcher judge
    loc = location.lower()
    if any(re.sub(r"\(.*?\)", "", no).strip().lower() in loc
           for no in profile.preferences.hard_nos if no.strip()):
        return False
    return any(ok.split("(")[0].strip().lower() in loc
               for ok in profile.preferences.locations_ok)


def classify(title: str, location: str, profile: Profile) -> tuple[str, str]:
    """Returns (state, reason)."""
    if not title_matches(title, profile):
        return REJECTED_AUTO, "title does not match target titles"
    if not location_ok(location, profile):
        return REJECTED_AUTO, f"location {location!r} outside preferences"
    return PREFILTERED, "title + location pass"


def run(conn, profile: Profile) -> dict:
    stats = {"seen": 0, "passed": 0, "rejected": 0}
    rows = conn.execute(
        "SELECT p.id, p.title, p.location FROM postings p "
        "WHERE p.closed_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM events e WHERE e.posting_id = p.id AND e.event_type LIKE 'prefilter:%')"
    ).fetchall()
    for row in rows:
        stats["seen"] += 1
        state, reason = classify(row["title"], row["location"] or "", profile)
        with tx(conn):
            log_event(conn, f"prefilter:{state}", posting_id=row["id"],
                      payload={"reason": reason})
        stats["passed" if state == PREFILTERED else "rejected"] += 1
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        profile = load_profile(settings.profile_path)
        stats = run(conn, profile)
        heartbeat(conn, "prefilter", ok=True, detail=str(stats))
        log.info("prefilter complete: %s", stats)
    except Exception as e:
        heartbeat(conn, "prefilter", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
