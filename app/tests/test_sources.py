"""Adapter contract: registry completeness, fixture normalization,
no-op-without-keys, aggregator polling via the runner."""

import json
import pathlib

import pytest

from jobpipe.config import settings
from jobpipe.pollers import runner
from jobpipe.sources import registry
from jobpipe.sources.base import SearchSpec, normalise_salary

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
ADZUNA = json.loads((FIXTURES / "adzuna_search.json").read_text())
REED = json.loads((FIXTURES / "reed_search.json").read_text())
SPEC = SearchSpec(source="x", name="toy-search", keywords="preschooler", location="London")


def test_registry_has_all_sources():
    assert set(registry.all_sources()) == {
        "greenhouse", "lever", "ashby", "workable", "builtin", "adzuna", "reed"}
    assert set(registry.aggregators()) == {"adzuna", "reed"}


def test_adzuna_normalize_fixture():
    a = registry.get("adzuna")
    dtos = [a.normalize(r, SPEC) for r in ADZUNA["results"]]
    d = dtos[0]
    assert d.title == "Trainee Preschooler"
    assert d.company_name == "Sunny Days Nursery Ltd"
    assert d.source == "adzuna" and (d.source_detail or "adzuna") == "adzuna"
    assert d.location == "London, Greater London"
    assert d.external_id == "5001234567"
    assert d.apply_url.startswith("https://www.adzuna.co.uk/jobs/land/ad/")
    assert d.raw["salary_normalised"] == {"min": 21000, "max": 24000,
                                          "currency": "GBP", "period": "year"}
    # missing salary -> None, not 0
    assert dtos[2].raw["salary_normalised"]["min"] is None


def test_reed_normalize_fixture():
    a = registry.get("reed")
    dtos = [a.normalize(r, SPEC) for r in REED["results"]]
    d = dtos[1]
    assert d.title == "Junior Tooth Fairy (Night Shift)"
    assert d.company_name == "Enamel Logistics"
    assert d.external_id == "88001002"
    assert d.location == "London, Central London"
    assert d.raw["salary_normalised"] == {"min": 26000, "max": None,
                                          "currency": "GBP", "period": "year"}


def test_normalise_salary_swaps_inverted_range():
    assert normalise_salary(70000, 55000) == {"min": 55000, "max": 70000,
                                              "currency": "GBP", "period": "year"}


def test_unconfigured_sources_noop(conn, monkeypatch):
    """No keys set -> aggregator run is a clean no-op with zero HTTP calls."""
    monkeypatch.setattr(settings, "adzuna_app_id", "")
    monkeypatch.setattr(settings, "adzuna_app_key", "")
    monkeypatch.setattr(settings, "reed_api_key", "")

    def boom(*a, **k):
        raise AssertionError("unconfigured source must not make HTTP calls")

    monkeypatch.setattr("jobpipe.pollers.base.polite_get", boom)
    searches = [{"name": "t", "source": "adzuna", "keywords": "data engineer"},
                {"name": "t2", "source": "reed", "keywords": "data engineer"}]
    stats = runner.run_aggregator_pollers(conn, searches)
    assert stats["adzuna"]["searches"] == 0 and stats["reed"]["searches"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"] == 0
    for h in registry.health(conn):
        if h["source"] in ("adzuna", "reed"):
            assert h["status"] == "unconfigured" and h["reason"]


def test_aggregator_poll_ingests_and_is_idempotent(conn, monkeypatch):
    monkeypatch.setattr(settings, "adzuna_app_id", "id")
    monkeypatch.setattr(settings, "adzuna_app_key", "key")
    monkeypatch.setattr(settings, "reed_api_key", "key")
    monkeypatch.setattr(registry.get("adzuna"), "fetch",
                        lambda spec: ADZUNA["results"])
    monkeypatch.setattr(registry.get("reed"), "fetch",
                        lambda spec: REED["results"])
    searches = [{"name": "toy-a", "source": "adzuna", "keywords": "preschooler"},
                {"name": "toy-r", "source": "reed", "keywords": "preschooler"}]
    s1 = runner.run_aggregator_pollers(conn, searches)
    assert s1["adzuna"]["new"] == 3
    # Reed's 3 jobs: 2 dedupe onto Adzuna postings (Preschooler, Cloud Watcher)
    assert s1["reed"]["new"] == 1
    n_postings = conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"]
    assert n_postings == 4
    n_prov = conn.execute("SELECT COUNT(*) c FROM source_postings").fetchone()["c"]
    assert n_prov == 6

    # Idempotency: re-running the same poll adds no rows anywhere.
    s2 = runner.run_aggregator_pollers(conn, searches)
    assert s2["adzuna"]["new"] == 0 and s2["reed"]["new"] == 0
    assert conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"] == n_postings
    assert conn.execute("SELECT COUNT(*) c FROM source_postings").fetchone()["c"] == n_prov


def test_adzuna_daily_call_cap(conn, monkeypatch):
    monkeypatch.setattr(settings, "adzuna_app_id", "id")
    monkeypatch.setattr(settings, "adzuna_app_key", "key")
    monkeypatch.setattr(settings, "adzuna_daily_call_cap", 1)
    monkeypatch.setattr(registry.get("adzuna"), "fetch", lambda spec: [])
    searches = [{"name": f"s{i}", "source": "adzuna", "keywords": "x"} for i in range(3)]
    stats = runner.run_aggregator_pollers(conn, searches)
    assert stats["adzuna"]["searches"] == 1 and stats["adzuna"]["skipped"] == 2


def test_parse_search_spec_legacy_builtin_entry():
    spec = runner.parse_search_spec(
        {"name": "de", "url": "https://builtinlondon.uk/jobs?search=data+engineer"})
    assert spec.source == "builtin" and spec.url.endswith("data+engineer")


def test_parse_search_spec_location_is_parameter():
    spec = runner.parse_search_spec(
        {"name": "m", "source": "reed", "keywords": "data engineer",
         "location": "Manchester", "distance_miles": 5})
    assert spec.location == "Manchester" and spec.distance_miles == 5


@pytest.mark.parametrize("name", ["greenhouse", "lever", "ashby", "workable"])
def test_ats_adapters_tag_provenance(name):
    from jobpipe.pollers import ashby, greenhouse, lever, workable
    raws = {
        "greenhouse": {"id": 1, "title": "T", "location": {"name": "London"},
                       "absolute_url": "https://x/1", "content": "d"},
        "lever": {"id": "1", "text": "T", "categories": {}, "hostedUrl": "https://x/1",
                  "descriptionPlain": "d", "description": "d"},
        "ashby": {"id": "1", "title": "T", "location": "London", "jobUrl": "https://x/1",
                  "descriptionHtml": "d"},
        "workable": {"shortcode": "1", "title": "T", "city": "London",
                     "url": "https://x/1", "description": "d"},
    }
    _ = (greenhouse, lever, ashby, workable)
    dto = registry.get(name).normalize(raws[name], SearchSpec(source=name, company_name="Acme"))
    assert dto.source == "ats" and dto.source_detail == name


# ---- live-recorded fixtures (created by scripts/record_fixtures.py) ------------------
# Skipped until the recordings exist; once committed, CI validates normalization
# against REAL payload shapes, not just the hand-written toys.

ADZUNA_LIVE = FIXTURES / "adzuna_search_live.json"
REED_LIVE = FIXTURES / "reed_search_live.json"


@pytest.mark.skipif(not ADZUNA_LIVE.exists(), reason="no live adzuna recording yet")
def test_adzuna_live_fixture_normalizes():
    a = registry.get("adzuna")
    raws = json.loads(ADZUNA_LIVE.read_text())["results"]
    assert raws
    for raw in raws:
        d = a.normalize(raw, SPEC)
        assert d.title and d.company_name and d.external_id and d.apply_url
        assert d.source == "adzuna"
        assert "salary_normalised" in d.raw


@pytest.mark.skipif(not REED_LIVE.exists(), reason="no live reed recording yet")
def test_reed_live_fixture_normalizes():
    a = registry.get("reed")
    raws = json.loads(REED_LIVE.read_text())["results"]
    assert raws
    for raw in raws:
        d = a.normalize(raw, SPEC)
        assert d.title and d.company_name and d.external_id and d.apply_url
        assert d.source == "reed"
        assert "salary_normalised" in d.raw
