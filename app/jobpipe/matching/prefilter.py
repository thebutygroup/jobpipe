"""Deterministic pre-filter: cut obviously irrelevant postings before any LLM spend.

Pass = title matches a target title/synonym AND location passes
(locations_ok match, or empty location = benefit of the doubt) AND no hard-no hit.

MULTI-USER: the verdict is the UNION over every active applicant's profile —
a posting passes if it is plausible for ANYONE. (Originally it judged against
the owner's file profile only, which silently blackholed every posting that
was relevant to a signup but not to the owner.) When the roster of active
profiles changes, previously-rejected postings are re-examined once — a new
signup must be able to "un-reject" the pond.
"""

from __future__ import annotations

import hashlib
import logging
import re

from ..config import settings
from ..db import connect, heartbeat, log_event, tx
from ..models import PREFILTERED, REJECTED_AUTO, normalise_title
from ..profile import Profile, ProfileError, load_applicant_profile, load_profile

log = logging.getLogger(__name__)

# NOTE: there used to be hardcoded DEFAULT_SYNONYMS here ("ai engineer",
# "fde", ...) from the single-user days. In a multi-profile world they made
# EVERY applicant title-match AI jobs — a Head of Data was scored against
# "Senior AI Engineer". They're gone: put them in the owner profile.yaml's
# preferences.title_synonyms where they belong.


def title_matches(title: str, profile: Profile) -> bool:
    norm = normalise_title(title)
    candidates = [normalise_title(t) for t in
                  profile.preferences.target_titles + profile.preferences.title_synonyms]
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


def classify_multi(title: str, location: str,
                   profiles: list[tuple[str, Profile]]) -> tuple[str, str]:
    """Union verdict: PREFILTERED if the posting passes for ANY active
    applicant, with the reason naming who it passed for."""
    for who, profile in profiles:
        state, reason = classify(title, location, profile)
        if state == PREFILTERED:
            return PREFILTERED, f"passes for {who}"
    return REJECTED_AUTO, "no active profile matches title/location"


def load_active_profiles(conn) -> list[tuple[str, Profile]]:
    """(who, Profile) for every active applicant; falls back to the file
    profile on a fresh DB (mirrors matcher bootstrap)."""
    out: list[tuple[str, Profile]] = []
    for row in conn.execute("SELECT * FROM applicants WHERE active = 1"
                            " ORDER BY id").fetchall():
        try:
            out.append((row["user_ref"] or row["name"], load_applicant_profile(row)))
        except (ProfileError, OSError) as e:
            log.warning("prefilter: skipping profile for %s (%s)", row["user_ref"], e)
    if not out:
        out.append(("owner", load_profile(settings.profile_path)))
    return out


def _roster_key(profiles: list[tuple[str, Profile]]) -> str:
    """Fingerprint of everything the verdict depends on. Changes when someone
    joins/leaves or edits titles/synonyms/locations/hard-nos."""
    parts = []
    for who, p in sorted(profiles, key=lambda x: x[0]):
        parts.append("|".join([who] + sorted(p.preferences.target_titles)
                              + sorted(p.preferences.title_synonyms)
                              + sorted(p.preferences.locations_ok)
                              + sorted(p.preferences.hard_nos)))
    return hashlib.sha256("||".join(parts).encode()).hexdigest()[:16]


def rescan_if_roster_changed(conn, profiles: list[tuple[str, Profile]]) -> int:
    """When the active-profile roster changes, clear previous REJECTED_AUTO
    verdicts so those postings get re-classified against the new union.
    Returns how many rejections were reopened."""
    key = _roster_key(profiles)
    row = conn.execute(
        "SELECT json_extract(payload_json,'$.key') AS k FROM events"
        " WHERE event_type='prefilter_roster' ORDER BY id DESC LIMIT 1").fetchone()
    if row and row["k"] == key:
        return 0
    with tx(conn):
        cur = conn.execute(
            "DELETE FROM events WHERE event_type = 'prefilter:REJECTED_AUTO'")
        log_event(conn, "prefilter_roster", payload={"key": key})
    if row is not None:  # not the first-ever run
        log.info("prefilter: roster changed — reopening %d rejected postings",
                 cur.rowcount)
    return cur.rowcount


def run(conn, profiles: list[tuple[str, Profile]] | Profile) -> dict:
    if isinstance(profiles, Profile):  # back-compat for single-profile callers
        profiles = [("owner", profiles)]
    stats = {"seen": 0, "passed": 0, "rejected": 0}
    rows = conn.execute(
        "SELECT p.id, p.title, p.location FROM postings p "
        "WHERE p.closed_at IS NULL AND NOT EXISTS ("
        "  SELECT 1 FROM events e WHERE e.posting_id = p.id AND e.event_type LIKE 'prefilter:%')"
    ).fetchall()
    for row in rows:
        stats["seen"] += 1
        state, reason = classify_multi(row["title"], row["location"] or "", profiles)
        with tx(conn):
            log_event(conn, f"prefilter:{state}", posting_id=row["id"],
                      payload={"reason": reason})
        stats["passed" if state == PREFILTERED else "rejected"] += 1
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        from . import title_expand
        added = title_expand.expand_all(conn)
        if added:
            log.info("title expansion added %d synonym(s) across profiles", added)
        profiles = load_active_profiles(conn)  # reloads expanded synonyms
        reopened = rescan_if_roster_changed(conn, profiles)
        stats = run(conn, profiles)
        stats["profiles"] = len(profiles)
        stats["reopened"] = reopened
        heartbeat(conn, "prefilter", ok=True, detail=str(stats))
        log.info("prefilter complete: %s", stats)
    except Exception as e:
        heartbeat(conn, "prefilter", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
