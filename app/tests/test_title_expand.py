"""Title expansion: model-generated similar titles land in title_synonyms,
exactly once per profile version, and immediately widen the prefilter."""

import json
from types import SimpleNamespace

import yaml

from jobpipe.matching import prefilter, title_expand


class FakeClient:
    def __init__(self, titles):
        self.titles = titles
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(
            type="text", text=json.dumps({"titles": self.titles}))])


def _add(conn, user_ref, titles, summary=""):
    profile_yaml = yaml.safe_dump({
        "identity": {"full_name": user_ref, "email": "u@e.com", "location": ""},
        "preferences": {"target_titles": titles, "locations_ok": ["London"]},
        "positioning_summary": summary,
    })
    conn.execute("INSERT INTO applicants (name, user_ref, profile_path,"
                 " profile_yaml, active) VALUES (?,?,'',?,1)",
                 (user_ref, user_ref, profile_yaml))
    conn.commit()
    return conn.execute("SELECT * FROM applicants WHERE user_ref=?",
                        (user_ref,)).fetchone()


def test_expansion_merges_and_is_idempotent(conn):
    row = _add(conn, "eeezee", ["Head of Data"],
               "Strategic data professional wanting to build a team")
    client = FakeClient(["Data Director", "Head of Analytics",
                         "Head of Data"])  # echo of a target gets dropped
    assert title_expand.expand_for_applicant(conn, row, client) == 2
    row = conn.execute("SELECT * FROM applicants WHERE user_ref='eeezee'").fetchone()
    prefs = yaml.safe_load(row["profile_yaml"])["preferences"]
    assert "Data Director" in prefs["title_synonyms"]
    # same profile version: no second model call
    assert title_expand.expand_for_applicant(conn, row, client) == 0
    assert client.calls == 1
    # profile changed -> expands again
    data = yaml.safe_load(row["profile_yaml"])
    data["preferences"]["target_titles"] = ["Head of Data", "CDO"]
    conn.execute("UPDATE applicants SET profile_yaml=? WHERE id=?",
                 (yaml.safe_dump(data), row["id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM applicants WHERE id=?", (row["id"],)).fetchone()
    title_expand.expand_for_applicant(conn, row, FakeClient(["VP Data"]))
    prefs = yaml.safe_load(conn.execute(
        "SELECT profile_yaml FROM applicants WHERE id=?",
        (row["id"],)).fetchone()["profile_yaml"])["preferences"]
    assert "VP Data" in prefs["title_synonyms"] and "Data Director" in prefs["title_synonyms"]


def test_garbage_model_output_filtered(conn):
    row = _add(conn, "x", ["Photographer"])
    client = FakeClient(["<script>alert(1)</script>", "", "ok title", 42,
                        "A" * 100])
    assert title_expand.expand_for_applicant(conn, row, client) == 1
    prefs = yaml.safe_load(conn.execute(
        "SELECT profile_yaml FROM applicants WHERE user_ref='x'"
    ).fetchone()["profile_yaml"])["preferences"]
    assert prefs["title_synonyms"] == ["ok title"]


def test_expanded_synonym_widens_prefilter(conn):
    row = _add(conn, "eeezee", ["Head of Data"])
    title_expand.expand_for_applicant(conn, row, FakeClient(["Data Director"]))
    profiles = prefilter.load_active_profiles(conn)
    state, reason = prefilter.classify_multi(
        "Data Director, EMEA", "London", profiles)
    assert state == "PREFILTERED" and "eeezee" in reason


def test_expand_all_skips_file_profiles_and_missing_key(conn, monkeypatch):
    from jobpipe.config import settings
    conn.execute("INSERT INTO applicants (name, user_ref, profile_path, active)"
                 " VALUES ('Owner','owner','profile.yaml',1)")
    conn.commit()
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    assert title_expand.expand_all(conn) == 0  # no key: silent no-op
    _add(conn, "maria", ["Photographer"])
    assert title_expand.expand_all(conn, client=FakeClient(["Retoucher"])) == 1


def test_scarcity_gate_respects_specific_titles_with_coverage(conn, monkeypatch):
    """Plenty of open postings matching their literal titles => no expansion
    (no model call). Thin coverage => expansion proceeds."""
    from jobpipe.config import settings
    from jobpipe.db import upsert_posting
    from jobpipe.models import PostingDTO

    monkeypatch.setattr(settings, "title_expand_when_below", 3)
    row = _add(conn, "specific", ["Platform Engineer"])
    for i in range(3):
        upsert_posting(conn, PostingDTO(
            company_name=f"C{i}", source="ats", external_id=f"pe{i}",
            title=f"Senior Platform Engineer {i}", location="London",
            apply_url=f"https://boards.greenhouse.io/c{i}/{i}",
            description_text="d"))
    conn.commit()
    client = FakeClient(["Infrastructure Engineer"])
    assert title_expand.expand_for_applicant(conn, row, client) == 0
    assert client.calls == 0  # respected: no model call at all
    # raise the bar so coverage is now "thin" -> expansion happens
    monkeypatch.setattr(settings, "title_expand_when_below", 10)
    assert title_expand.expand_for_applicant(conn, row, client) == 1
    assert client.calls == 1
