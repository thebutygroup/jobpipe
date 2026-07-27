"""Multi-user relevance & source tiers: profile-derived searches, union
prefilter with roster rescan, and the matcher's tier-1-first ordering."""

import yaml

from jobpipe import profile_searches
from jobpipe.matching import matcher, prefilter


def _add_applicant(conn, user_ref, titles, email="u@example.com",
                   locations=None, active=1):
    profile_yaml = yaml.safe_dump({
        "identity": {"full_name": user_ref.title(), "email": email,
                     "location": "London"},
        "preferences": {"target_titles": titles,
                        "locations_ok": locations or ["London"]},
    })
    conn.execute("INSERT INTO applicants (name, user_ref, profile_path,"
                 " profile_yaml, active) VALUES (?,?,'',?,?)",
                 (user_ref.title(), user_ref, profile_yaml, active))
    conn.commit()


# ---- profile-derived searches ----------------------------------------------

def test_derive_covers_all_query_sources_and_dedupes(conn, monkeypatch):
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "disabled_sources", "")
    _add_applicant(conn, "maria", ["Photographer and retoucher"])
    _add_applicant(conn, "chris", ["Business Development"])
    existing = [{"name": "hand", "source": "reed",
                 "keywords": "business development", "location": "London"}]
    derived = profile_searches.derive_profile_searches(conn, existing)
    by = {(d["source"], d["keywords"]) for d in derived}
    # maria gets all three query sources
    assert ("adzuna", "photographer and retoucher") in by
    assert ("reed", "photographer and retoucher") in by
    assert ("builtin", "photographer and retoucher") in by
    # chris's reed search deduped against the hand-written entry
    assert ("reed", "business development") not in by
    assert ("adzuna", "business development") in by
    # builtin entries carry a constructed saved-search URL
    bi = next(d for d in derived if d["source"] == "builtin")
    assert bi["url"].startswith("https://builtinlondon.uk/jobs?search=")


def test_derive_skips_inactive_disabled_and_caps(conn, monkeypatch):
    from jobpipe.config import settings
    _add_applicant(conn, "ghost", ["Ghost Hunter"], active=0)
    _add_applicant(conn, "maria", ["Photographer"])
    monkeypatch.setattr(settings, "disabled_sources", "adzuna")
    derived = profile_searches.derive_profile_searches(conn, [])
    assert all(d["source"] != "adzuna" for d in derived)
    assert all("ghost" not in d["keywords"] for d in derived)
    monkeypatch.setattr(settings, "disabled_sources", "")
    assert len(profile_searches.derive_profile_searches(conn, [], cap=1)) == 1


# ---- union prefilter + roster rescan ---------------------------------------

def test_union_prefilter_passes_for_any_profile(conn, profile):
    profiles = [("owner", profile)]
    _add_applicant(conn, "maria", ["Photographer"])
    profiles = prefilter.load_active_profiles(conn)
    state, reason = prefilter.classify_multi("Senior Photographer", "London",
                                            profiles)
    assert state == "PREFILTERED" and "maria" in reason
    state, _ = prefilter.classify_multi("Senior Data Engineer", "London",
                                        [("owner", profile)] + profiles)
    assert state == "PREFILTERED"
    state, _ = prefilter.classify_multi("Head Chef", "London", profiles)
    assert state == "REJECTED_AUTO"


def test_roster_change_reopens_rejections(conn, profile):
    from jobpipe.db import log_event
    # a photography posting rejected under the old owner-only prefilter
    conn.execute("INSERT INTO companies (name, ats) VALUES ('PhotoCo','custom')")
    from jobpipe.db import upsert_posting
    from jobpipe.models import PostingDTO
    upsert_posting(conn, PostingDTO(
        company_name="PhotoCo", source="reed", external_id="x1",
        title="Photographer", location="London",
        apply_url="https://reed.example/x1", description_text="d"))
    owner = [("owner", profile)]
    prefilter.rescan_if_roster_changed(conn, owner)  # first-ever run (migration)
    log_event(conn, "prefilter:REJECTED_AUTO", posting_id=1)
    conn.commit()
    assert prefilter.rescan_if_roster_changed(conn, owner) == 0  # unchanged
    _add_applicant(conn, "maria", ["Photographer"])
    profiles = prefilter.load_active_profiles(conn)
    reopened = prefilter.rescan_if_roster_changed(conn, profiles)
    assert reopened >= 1  # the photography rejection is back in play
    stats = prefilter.run(conn, profiles)
    assert stats["passed"] >= 1  # ...and now passes via maria


# ---- matcher source tiers ---------------------------------------------------

def _row(source, i):
    return {"source": source, "id": f"{source}{i}"}


def test_interleave_round_robins_sources():
    rows = ([_row("adzuna", i) for i in range(5)]
            + [_row("builtin", i) for i in range(2)]
            + [_row("ats", i) for i in range(2)])
    out = matcher.interleave_by_source(rows)
    first_three = {r["source"] for r in out[:3]}
    assert first_three == {"adzuna", "builtin", "ats"}
    assert len(out) == 9


def test_order_pending_holds_secondary_back(monkeypatch):
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "secondary_sources", "adzuna")
    rows = [_row("adzuna", 1), _row("builtin", 1), _row("adzuna", 2),
            _row("ats", 1)]
    primary, secondary = matcher.order_pending(rows)
    assert {r["source"] for r in primary} == {"builtin", "ats"}
    assert all(r["source"] == "adzuna" for r in secondary)
