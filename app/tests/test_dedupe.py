"""Cross-source identity resolution: the nasty cases."""

from jobpipe import dedupe
from jobpipe.db import upsert_posting
from jobpipe.models import PostingDTO


def dto(company="Sunny Days Nursery", source="adzuna", source_detail="",
        external_id="1", title="Trainee Preschooler", location="London",
        url="https://a/1", description="d"):
    return PostingDTO(company_name=company, source=source, external_id=external_id,
                      title=title, location=location, apply_url=url,
                      description_text=description,
                      source_detail=source_detail or source)


# ---- unit: heuristics ----------------------------------------------------------------

def test_normalise_company_strips_legal_suffixes():
    assert dedupe.normalise_company("Sunny Days Nursery Ltd.") == \
        dedupe.normalise_company("Sunny Days Nursery")
    assert dedupe.normalise_company("The Acme Group PLC") == "acme"
    assert dedupe.normalise_company("Blue Sky Research Ltd") == "blue sky research"


def test_titles_match_punctuation_variants():
    ok, rule, _ = dedupe.titles_match("Trainee Pre-Schooler", "Trainee Preschooler")
    assert ok  # hyphenation difference
    ok, rule, _ = dedupe.titles_match("Professional Cloud-Watcher",
                                      "Professional Cloud Watcher")
    assert ok and rule == "title_exact"  # punctuation normalises away
    ok, _, _ = dedupe.titles_match("Senior Data Engineer", "Senior Data Scientist")
    assert not ok  # different job, similar words


def test_locations_compatible():
    assert dedupe.locations_compatible("London", "London, Greater London")
    assert dedupe.locations_compatible("", "London")  # empty side is permissive
    assert not dedupe.locations_compatible("Manchester", "London")


# ---- integration: ingest-time dedupe -------------------------------------------------

def test_same_job_via_aggregator_and_ats_links(conn):
    """ATS posting exists; the aggregator copy must attach, not duplicate."""
    ats_id, new = upsert_posting(conn, dto(
        company="Sunny Days Nursery", source="ats", source_detail="greenhouse",
        title="Trainee Preschooler", url="https://boards.greenhouse.io/sunny/1"))
    assert new
    agg_id, new = upsert_posting(conn, dto(
        company="Sunny Days Nursery Ltd", source="adzuna",
        title="Trainee Pre-Schooler", url="https://adzuna.co.uk/land/ad/9"))
    assert not new and agg_id == ats_id
    sources = {r["source"] for r in conn.execute(
        "SELECT source FROM source_postings WHERE posting_id = ?", (ats_id,))}
    assert sources == {"greenhouse", "adzuna"}
    # merge is auditable
    ev = conn.execute("SELECT payload_json FROM events "
                      "WHERE event_type='dedupe_linked'").fetchone()
    assert ev and "title" in ev["payload_json"]


def test_ats_arriving_later_promotes_canonical(conn):
    """Aggregator seen first; ATS copy later becomes the canonical record."""
    agg_id, _ = upsert_posting(conn, dto(
        company="Sunny Days Nursery Ltd", source="adzuna",
        title="Trainee Pre-Schooler", url="https://adzuna.co.uk/land/ad/9",
        description="short snippet"))
    ats_id, new = upsert_posting(conn, dto(
        company="Sunny Days Nursery", source="ats", source_detail="greenhouse",
        title="Trainee Preschooler", url="https://boards.greenhouse.io/sunny/1",
        description="the full description, much longer than the snippet"))
    assert not new and ats_id == agg_id
    row = conn.execute("SELECT source, apply_url, description_text FROM postings "
                       "WHERE id = ?", (agg_id,)).fetchone()
    assert row["source"] == "ats"
    assert "greenhouse.io" in row["apply_url"]
    assert "full description" in row["description_text"]  # longer text won
    # and the ATS identity is now the exact key: re-poll hits path 1
    again, new = upsert_posting(conn, dto(
        company="Sunny Days Nursery", source="ats", source_detail="greenhouse",
        title="Trainee Preschooler", url="https://boards.greenhouse.io/sunny/1"))
    assert again == agg_id and not new


def test_different_locations_do_not_merge(conn):
    a, _ = upsert_posting(conn, dto(title="Trainee Preschooler", location="London",
                                    url="https://a/1", external_id="1"))
    b, new = upsert_posting(conn, dto(title="Trainee Preschooler", location="Manchester",
                                      url="https://a/2", external_id="2", source="reed"))
    assert new and b != a


def test_different_roles_same_company_do_not_merge(conn):
    a, _ = upsert_posting(conn, dto(title="Senior Data Engineer", url="https://a/1",
                                    external_id="1"))
    b, new = upsert_posting(conn, dto(title="Senior Data Scientist", url="https://a/2",
                                      external_id="2"))
    assert new and b != a


def test_reingest_same_source_is_idempotent(conn):
    a, new1 = upsert_posting(conn, dto())
    b, new2 = upsert_posting(conn, dto())
    assert new1 and not new2 and a == b
    assert conn.execute("SELECT COUNT(*) c FROM source_postings").fetchone()["c"] == 1


def test_aggregator_snippet_never_overwrites_full_description(conn):
    upsert_posting(conn, dto(
        company="Sunny Days Nursery", source="ats", source_detail="greenhouse",
        title="Trainee Preschooler", url="https://boards.greenhouse.io/sunny/1",
        description="the full description, much longer than the snippet"))
    pid, _ = upsert_posting(conn, dto(
        company="Sunny Days Nursery Ltd", source="reed",
        title="Trainee Pre-Schooler", url="https://reed.co.uk/jobs/9",
        description="snippet"))
    row = conn.execute("SELECT description_text FROM postings WHERE id = ?",
                       (pid,)).fetchone()
    assert "full description" in row["description_text"]
