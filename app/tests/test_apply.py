"""Application flow Phase 1: route resolution, asset vault, the aggregate."""

import json

import pytest

from jobpipe.apply import routes
from jobpipe.apply.models import Application
from jobpipe.apply.platforms import get_applier
from jobpipe.apply.vault import AssetVault, VaultError
from jobpipe.db import upsert_posting
from jobpipe.models import PostingDTO

# ---- classification (pure, no network) -----------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://boards.greenhouse.io/fireworks/jobs/123", "greenhouse"),
    ("https://job-boards.greenhouse.io/acme/jobs/9", "greenhouse"),
    ("https://jobs.lever.co/acme/uuid-1", "lever"),
    ("https://jobs.ashbyhq.com/acme/uuid", "ashby"),
    ("https://apply.workable.com/acme/j/AB12/", "workable"),
    ("https://www.reed.co.uk/jobs/data-engineer/1", "login_walled"),
    ("https://uk.linkedin.com/jobs/view/1", "login_walled"),
    ("https://builtinlondon.uk/job/ml-engineer/10326827", "router:builtin"),
    ("https://www.adzuna.co.uk/jobs/land/ad/500123", "router:adzuna"),
    ("https://careers.acme.example/openings/42", "company_site"),
    ("", "invalid"),
])
def test_classify(url, expected):
    assert routes.classify(url) == expected


# ---- resolution chains (injected fetchers, no network) -------------------------------

def test_builtin_routes_to_greenhouse():
    r = routes.resolve_route(
        "https://builtinlondon.uk/job/ml-engineer/1",
        builtin_resolve=lambda u: "https://boards.greenhouse.io/fireworks/jobs/5")
    assert r.platform == "greenhouse" and r.method == routes.BROWSER_FORM
    assert r.hops == ["https://builtinlondon.uk/job/ml-engineer/1",
                      "https://boards.greenhouse.io/fireworks/jobs/5"]


def test_adzuna_redirect_to_lever():
    r = routes.resolve_route(
        "https://www.adzuna.co.uk/jobs/land/ad/1",
        follow=lambda u: "https://jobs.lever.co/acme/xyz")
    assert r.platform == "lever" and len(r.hops) == 2


def test_builtin_login_wall_becomes_manual_assist():
    r = routes.resolve_route("https://builtinlondon.uk/job/x/1",
                             builtin_resolve=lambda u: "")
    assert r.method == routes.MANUAL_ASSIST and r.platform == "login_walled"


def test_reed_is_manual_assist():
    r = routes.resolve_route("https://www.reed.co.uk/jobs/x/9")
    assert r.method == routes.MANUAL_ASSIST and r.platform == "login_walled"


def test_unknown_company_site_uses_browser_form():
    r = routes.resolve_route("https://careers.acme.example/openings/42")
    assert r.platform == "company_site" and r.method == routes.BROWSER_FORM


def test_router_loop_gives_up():
    # adzuna that redirects to itself forever
    r = routes.resolve_route("https://www.adzuna.co.uk/jobs/land/ad/1",
                             follow=lambda u: u + "x" if "adzuna" in u else u)
    assert r.method == routes.MANUAL_ASSIST


def test_ensure_route_persists_and_caches(conn):
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name="Fireworks AI", source="builtin", external_id="1",
        title="Applied ML Engineer", location="London",
        apply_url="https://builtinlondon.uk/job/ml/1"))
    calls = []

    def fake_builtin(u):
        calls.append(u)
        return "https://boards.greenhouse.io/fireworks/jobs/5"

    r1 = routes.ensure_route(conn, pid, builtin_resolve=fake_builtin)
    assert r1.platform == "greenhouse" and len(calls) == 1
    r2 = routes.ensure_route(conn, pid)          # cached: no resolver needed
    assert r2.platform == "greenhouse" and r2.final_url == r1.final_url
    row = conn.execute("SELECT * FROM apply_routes WHERE posting_id = ?",
                       (pid,)).fetchone()
    assert row and json.loads(row["hops_json"])[0].startswith("https://builtinlondon")


# ---- platform registry ---------------------------------------------------------------

def test_registry_platform_knowledge():
    assert get_applier("greenhouse").name == "greenhouse"
    assert get_applier("ashby").needs_browser is True
    assert get_applier("workable").needs_browser is True
    assert get_applier("company_site").name == "generic"   # fallback
    assert get_applier("login_walled").name == "generic"


# ---- vault ---------------------------------------------------------------------------

@pytest.fixture()
def vault(conn, tmp_path, monkeypatch):
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "assets_root", str(tmp_path / "assets"))
    conn.execute("INSERT INTO applicants (name, profile_path, user_ref)"
                 " VALUES ('joebuty', 'p', 'joebuty')")
    conn.commit()
    return AssetVault(conn, 1)


def test_vault_add_and_variant_selection(vault, tmp_path):
    (tmp_path / "cv.pdf").write_bytes(b"%PDF- default cv")
    (tmp_path / "cv-mlops.pdf").write_bytes(b"%PDF- mlops cv")
    vault.add(str(tmp_path / "cv.pdf"))
    vault.add(str(tmp_path / "cv-mlops.pdf"), variant_name="mlops")
    assert len(vault.list()) == 2
    # variant beats default when the title mentions it
    assert vault.resume_for("Senior MLOps Engineer").name == "resume-mlops.pdf"
    assert vault.resume_for("Forward Deployed Engineer").name == "resume-default.pdf"
    # files live under the random token dir, never a guessable name
    assert vault.token in str(vault.resume_for("x"))


def test_vault_rejects_bad_types_and_replaces(vault, tmp_path):
    (tmp_path / "cv.exe").write_bytes(b"nope")
    with pytest.raises(VaultError):
        vault.add(str(tmp_path / "cv.exe"))
    (tmp_path / "a.pdf").write_bytes(b"one")
    (tmp_path / "b.pdf").write_bytes(b"two")
    vault.add(str(tmp_path / "a.pdf"))
    vault.add(str(tmp_path / "b.pdf"))            # same kind+variant -> replace
    assert len(vault.list()) == 1
    assert vault.resume_for("x").read_bytes() == b"two"


def test_vault_empty_returns_none(vault):
    assert vault.resume_for("anything") is None


# ---- the aggregate -------------------------------------------------------------------

def test_application_aggregate_loads(conn, tmp_path, monkeypatch):
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "assets_root", str(tmp_path / "assets"))
    conn.execute(
        "INSERT INTO applicants (name, profile_path, user_ref, profile_yaml) VALUES"
        " ('joebuty', '', 'joebuty', 'identity:\n  full_name: joebuty\n"
        "preferences:\n  target_titles: [ML Engineer]\n  locations_ok: [London]\n"
        "positioning_summary: ships things')")
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name="Fireworks AI", source="builtin", external_id="1",
        title="Applied MLOps Engineer", location="London",
        apply_url="https://builtinlondon.uk/job/ml/1"))
    conn.execute("INSERT INTO applications (posting_id, applicant_id, state,"
                 " created_at, updated_at) VALUES (?,1,'MATCHED',datetime('now'),"
                 " datetime('now'))", (pid,))
    conn.commit()
    (tmp_path / "cv-mlops.pdf").write_bytes(b"%PDF-")
    AssetVault(conn, 1).add(str(tmp_path / "cv-mlops.pdf"), variant_name="mlops")

    app = Application.load(
        conn, 1, builtin_resolve=lambda u: "https://boards.greenhouse.io/fw/jobs/5")
    assert app.job.company == "Fireworks AI"
    assert app.job.route.platform == "greenhouse"
    assert app.job.applier.name == "greenhouse"
    assert app.applicant.user_ref == "joebuty"
    assert app.resume_path.name == "resume-mlops.pdf"   # variant matched the title
    assert app.state == "MATCHED"
