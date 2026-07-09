"""Weekly crawl of bespoke careers sites (and resolved dream companies).

For each target: careers page -> JSON-LD on the page itself -> else discover
job links (CRAWL_PAGE_CAP) -> JSON-LD per page -> else Haiku fallback
(CRAWL_LLM_CAP per whole run). Postings upsert with source='ats' ("from the
company's own site") so cross-source linking upgrades Built In twins.
"""
from __future__ import annotations

import logging

from ..config import settings
from ..db import connect, get_or_create_company, log_event, tx, upsert_posting
from ..models import PostingDTO
from ..pollers.base import FetchError, polite_get
from . import discover, jsonld

log = logging.getLogger(__name__)


def targets(conn) -> list:
    """Bespoke-classified companies with a careers URL (index or dream feed)."""
    return conn.execute(
        "SELECT name, careers_url FROM index_companies "
        "WHERE status = 'bespoke' AND careers_url IS NOT NULL AND careers_url != ''"
    ).fetchall()


def _to_dto(company: str, job: dict, page_url: str) -> PostingDTO:
    return PostingDTO(
        company_name=company,
        source="ats",
        external_id=job.get("apply_url") or page_url,
        title=job["title"],
        location=job.get("location", ""),
        apply_url=job.get("apply_url") or page_url,
        description_text=(job.get("description") or "")[:20000],
        raw={"crawler": True, "page_url": page_url,
             "employment_type": job.get("employment_type", ""),
             "date_posted": job.get("date_posted", "")},
    )


def crawl_company(conn, name: str, careers_url: str, client, llm_budget: list[int]) -> dict:
    stats = {"jsonld": 0, "llm": 0, "pages": 0}
    if not discover.allowed_by_robots(careers_url, polite_get):
        log.info("robots.txt disallows %s; skipping", careers_url)
        return stats
    try:
        listing_html = polite_get(careers_url).text
    except FetchError:
        return stats

    jobs = jsonld.extract_jobpostings(listing_html, careers_url)
    pages: list[tuple[str, str]] = []
    if not jobs:
        for url in discover.find_job_links(careers_url, listing_html, settings.crawl_page_cap):
            try:
                pages.append((url, polite_get(url).text))
                stats["pages"] += 1
            except FetchError:
                continue
        for url, html in pages:
            found = jsonld.extract_jobpostings(html, url)
            if found:
                jobs.extend(found)
                stats["jsonld"] += len(found)
            elif client is not None and llm_budget[0] > 0:
                llm_budget[0] -= 1
                job = discover.llm_extract(client, url, html)
                if job:
                    jobs.append(job)
                    stats["llm"] += 1
    else:
        stats["jsonld"] = len(jobs)

    company_id = get_or_create_company(conn, name, ats="crawl",
                                       notes="bespoke careers site (crawler)")
    with tx(conn):
        for job in jobs:
            dto = _to_dto(name, job, careers_url)
            upsert_posting(conn, dto, company_id=company_id)
    return stats


def main() -> None:
    if not settings.crawl_enabled:
        log.info("crawler disabled (CRAWL_ENABLED=false)")
        return
    conn = connect()
    client = None
    if settings.anthropic_api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    llm_budget = [settings.crawl_llm_cap]
    totals = {"companies": 0, "jsonld": 0, "llm": 0, "pages": 0}
    for row in targets(conn):
        s = crawl_company(conn, row["name"], row["careers_url"], client, llm_budget)
        totals["companies"] += 1
        for k in ("jsonld", "llm", "pages"):
            totals[k] += s[k]
    with tx(conn):
        log_event(conn, "crawl_run", payload=totals)
    log.info("crawl complete: %s", totals)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    main()
