from jobpipe.indexscan import workday
from jobpipe.indexscan.constituents import load_static, parse_constituents
from jobpipe.indexscan.resolver import classify_html, detect_workday
from jobpipe.indexscan.runner import resolve_batch, sync_constituents

FTSE_HTML = """
<table class="wikitable"><tr><th>Company</th><th>Ticker</th><th>Industry</th></tr>
""" + "\n".join(f"<tr><td>Company {i}</td><td>C{i}</td><td>Tech</td></tr>"
                for i in range(60)) + "</table>"


def test_parse_constituents_finds_big_table():
    names = parse_constituents(FTSE_HTML, "ftse100")
    assert len(names) == 60 and names[0] == "Company 0"


def test_parse_rejects_small_tables():
    tiny = """<table class="wikitable"><tr><th>Company</th></tr>
              <tr><td>Only One</td></tr></table>"""
    assert parse_constituents(tiny, "ftse100") == []


def test_static_fallback_loads():
    data = load_static("constituents_static.yaml")
    assert len(data["ftse100"]) >= 20 and "AstraZeneca" in data["ftse100"]


def test_detect_workday():
    r = detect_workday("https://acme.wd3.myworkdayjobs.com/en-GB/AcmeCareers/job/x")
    assert r["workday_tenant"] == "acme" and r["workday_site"] == "AcmeCareers"
    assert detect_workday("https://boards.greenhouse.io/acme") == {}


def test_classify_ats_link_in_page():
    html = '<a href="https://boards.greenhouse.io/acmecorp">See open roles</a>'
    r = classify_html("https://acme.com/careers", html)
    assert r == {"status": "resolved_ats", "ats": "greenhouse", "board_token": "acmecorp"}


def test_classify_workday_page():
    html = '<a href="https://acme.wd1.myworkdayjobs.com/External">Search jobs</a>'
    r = classify_html("https://acme.com/careers", html)
    assert r["status"] == "workday" and r["workday_tenant"] == "acme"


def test_classify_bespoke_flags_not_scrapes():
    html = '<script src="https://acme.successfactors.eu/widget.js"></script>'
    r = classify_html("https://acme.com/careers", html)
    assert r["status"] == "bespoke" and "v2 agent" in r["notes"]


def test_workday_normalise():
    dto = workday.normalise("Acme", "https://acme.wd1.myworkdayjobs.com", "External",
                            {"title": "Senior Data Engineer",
                             "locationsText": "London, UK",
                             "externalPath": "/job/London/Senior-DE_R123",
                             "bulletFields": ["R123"]})
    assert dto.external_id == "R123"
    assert dto.apply_url.endswith("/External/job/London/Senior-DE_R123")


def test_sync_and_capped_resolution(conn, monkeypatch):
    import jobpipe.indexscan.constituents as cmod
    import jobpipe.indexscan.runner as rmod

    monkeypatch.setattr(cmod, "fetch_index",
                        lambda idx: [f"{idx}-co-{i}" for i in range(5)])
    stats = sync_constituents(conn)
    assert stats["ftse100"] >= 5 and stats["sp500"] >= 5
    # resolution capped at 3, all classified bespoke via stubbed resolver
    monkeypatch.setattr(rmod, "resolve_company",
                        lambda name, domain="", **kw: {"status": "bespoke", "domain": "x.com",
                                                 "careers_url": "https://x.com/careers",
                                                 "notes": "v2 agent scope"})
    r = resolve_batch(conn, cap=3)
    assert r["bespoke"] == 3
    remaining = conn.execute("SELECT COUNT(*) c FROM index_companies "
                             "WHERE status='new'").fetchone()["c"]
    assert remaining >= 7  # cap respected, rest untouched


def test_resolved_ats_enters_main_registry(conn, monkeypatch):
    import jobpipe.indexscan.constituents as cmod
    import jobpipe.indexscan.runner as rmod

    monkeypatch.setattr(cmod, "fetch_index", lambda idx: ["AtsCo"] if idx == "ftse100" else [])
    sync_constituents(conn)
    monkeypatch.setattr(rmod, "resolve_company",
                        lambda name, domain="", **kw: {"status": "resolved_ats",
                                                 "ats": "greenhouse", "board_token": "atsco",
                                                 "domain": "atsco.com",
                                                 "careers_url": "https://atsco.com/careers"})
    resolve_batch(conn, cap=10)
    row = conn.execute("SELECT ats, board_token, notes FROM companies "
                       "WHERE name='AtsCo'").fetchone()
    assert row and row["ats"] == "greenhouse" and "index scan" in row["notes"]
