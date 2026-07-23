"""Poll runner: registry sync -> ATS pollers -> Built In saved searches ->
aggregator APIs (Adzuna, Reed).

All sources go through the adapter registry (jobpipe.sources). Per-source
failure isolation: one dead board, search or API logs an error and the run
continues. Unconfigured aggregators (no API key yet) no-op with a clear log
line and show as 'unconfigured' in registry.health(). Postings unseen for 2
consecutive runs get closed_at set.
"""

from __future__ import annotations

import logging

import yaml

from ..config import settings
from ..db import connect, get_or_create_company, heartbeat, log_event, now, tx, upsert_posting
from ..sources import registry
from ..sources.base import SearchSpec
from . import builtin

log = logging.getLogger(__name__)
VALID_ATS = set(registry.ATS_NAMES) | {"custom"}


def sync_registry(conn, companies_path: str) -> None:
    with open(companies_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    for c in data.get("companies", []):
        ats = c.get("ats", "custom")
        if ats not in VALID_ATS:
            raise ValueError(f"unknown ats {ats!r} for company {c.get('name')!r}")
        with tx(conn):
            cid = get_or_create_company(conn, c["name"], ats=ats,
                                        board_token=c.get("board_token"),
                                        priority=c.get("priority", 3))
            conn.execute(
                "UPDATE companies SET ats = ?, board_token = ?, priority = ? WHERE id = ?",
                (ats, c.get("board_token"), c.get("priority", 3), cid),
            )


def load_searches(searches_path: str) -> list[dict]:
    with open(searches_path, "r", encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("searches", [])


def _within_cooldown(last_polled_at: str | None) -> bool:
    """True if last_polled_at is within poll_cooldown_days (so we should skip)."""
    if settings.poll_cooldown_days <= 0 or not last_polled_at:
        return False
    import datetime as dt

    try:
        last = dt.datetime.fromisoformat(last_polled_at)
    except ValueError:
        return False
    age_days = (dt.datetime.now() - last).total_seconds() / 86400.0
    return age_days < settings.poll_cooldown_days


def run_ats_pollers(conn) -> dict:
    stats = {"companies": 0, "postings": 0, "new": 0, "errors": 0, "skipped_cooldown": 0}
    per_source: dict[str, dict] = {}
    rows = conn.execute(
        "SELECT id, name, ats, board_token, last_polled_at FROM companies "
        "WHERE ats != 'custom' AND board_token IS NOT NULL"
    ).fetchall()
    for row in rows:
        if _within_cooldown(row["last_polled_at"]):
            stats["skipped_cooldown"] += 1
            continue
        stats["companies"] += 1
        ps = per_source.setdefault(row["ats"], {"postings": 0, "new": 0, "errors": 0})
        try:
            spec = SearchSpec(source=row["ats"], company_name=row["name"],
                              board_token=row["board_token"])
            dtos = registry.get(row["ats"]).fetch_postings(spec)
            if settings.poll_test_limit > 0:
                dtos = dtos[:settings.poll_test_limit]
        except Exception:
            stats["errors"] += 1
            ps["errors"] += 1
            log.exception("poller failed for %s (%s)", row["name"], row["ats"])
            continue
        with tx(conn):
            for dto in dtos:
                _, is_new = upsert_posting(conn, dto, company_id=row["id"])
                stats["postings"] += 1
                stats["new"] += int(is_new)
                ps["postings"] += 1
                ps["new"] += int(is_new)
            conn.execute("UPDATE companies SET last_polled_at = ? WHERE id = ?",
                         (now(), row["id"]))
    with tx(conn):
        for source, ps in per_source.items():
            log_event(conn, "source_polled", payload={"source": source, **ps})
    return stats


def _aggregator_calls_today(conn, source: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type = 'aggregator_call' "
        "AND json_extract(payload_json, '$.source') = ? AND created_at >= date('now')",
        (source,)).fetchone()["n"]


def run_aggregator_pollers(conn, searches: list[dict]) -> dict:
    """Poll API aggregators (Adzuna, Reed) for each configured search.

    No-op-without-keys: an unconfigured source logs ONE clear line and is
    skipped; registry.health() reports it as 'unconfigured'. Adzuna's free
    tier is rate-limited, so calls are counted (aggregator_call events) and
    capped per day."""
    stats: dict[str, dict] = {}
    specs = [s for s in (parse_search_spec(raw) for raw in searches)
             if s and s.source in registry.aggregators()]
    by_source: dict[str, list[SearchSpec]] = {}
    for spec in specs:
        by_source.setdefault(spec.source, []).append(spec)
    for source, adapter in registry.aggregators().items():
        source_specs = by_source.get(source, [])
        st = stats[source] = {"searches": 0, "postings": 0, "new": 0, "errors": 0,
                              "skipped": 0}
        if not adapter.is_configured():
            log.warning("source %s unconfigured — skipping %d search(es): %s",
                        source, len(source_specs), adapter.unconfigured_reason())
            continue
        cap = settings.adzuna_daily_call_cap if source == "adzuna" else 10**9
        for spec in source_specs:
            if _aggregator_calls_today(conn, source) >= cap:
                st["skipped"] += 1
                log.warning("%s daily call cap (%d) reached; skipping %s",
                            source, cap, spec.label)
                continue
            st["searches"] += 1
            with tx(conn):
                log_event(conn, "aggregator_call", payload={"source": source,
                                                            "search": spec.label})
            try:
                dtos = adapter.fetch_postings(spec)
                if settings.poll_test_limit > 0:
                    dtos = dtos[:settings.poll_test_limit]
            except Exception:
                st["errors"] += 1
                log.exception("%s search failed: %s", source, spec.label)
                continue
            with tx(conn):
                for dto in dtos:
                    _, is_new = upsert_posting(conn, dto)
                    st["postings"] += 1
                    st["new"] += int(is_new)
        with tx(conn):
            log_event(conn, "source_polled", payload={"source": source, **st})
    return stats


def parse_search_spec(raw: dict) -> SearchSpec | None:
    """searches.yaml entry -> SearchSpec. Entries with a url (and no explicit
    source) are Built In saved searches — the pre-multi-source format."""
    source = raw.get("source") or ("builtin" if raw.get("url") else None)
    if source is None:
        log.warning("search entry %r has no source and no url; skipping", raw.get("name"))
        return None
    return SearchSpec(
        source=source, name=raw.get("name", ""), url=raw.get("url", ""),
        keywords=raw.get("keywords", ""),
        location=raw.get("location", "London"),
        distance_miles=int(raw.get("distance_miles", 15)))


def _search_within_cooldown(conn, search_name: str) -> bool:
    if settings.poll_cooldown_days <= 0:
        return False
    row = conn.execute(
        "SELECT created_at FROM events WHERE event_type = 'builtin_search_polled' "
        "AND json_extract(payload_json, '$.name') = ? ORDER BY id DESC LIMIT 1",
        (search_name,)).fetchone()
    return _within_cooldown(row["created_at"]) if row else False



def run_builtin_poller(conn, searches: list[dict]) -> dict:
    stats = {"searches": 0, "postings": 0, "new": 0, "errors": 0, "companies_discovered": 0,
             "skipped_cooldown": 0}
    searches = [s for s in searches
                if (s.get("source") or ("builtin" if s.get("url") else "")) == "builtin"]
    if not searches:
        return stats
    if not builtin.robots_allows():
        log.warning("builtinlondon.uk robots.txt disallows /jobs; skipping Built In channel")
        return stats
    for search in searches:
        if _search_within_cooldown(conn, search.get("name", search["url"])):
            stats["skipped_cooldown"] += 1
            continue
        stats["searches"] += 1
        try:
            dtos = builtin.fetch_search(search["url"], max_pages=settings.builtin_max_pages)
        except Exception:
            stats["errors"] += 1
            log.exception("builtin search failed: %s", search.get("name"))
            continue
        for dto in dtos:
            try:
                detail = builtin.resolve_job_detail(dto.raw["builtin_job_url"])
                dto.apply_url = detail["external_apply_url"] or dto.raw["builtin_job_url"]
                if detail["full_description"]:
                    dto.description_text = detail["full_description"][:20000]
                if detail["company_page_url"]:
                    dto.raw["builtin_company_url"] = detail["company_page_url"]
                ats_info = detail["ats_info"]
            except Exception:
                log.warning("could not resolve job detail for %s; keeping card data",
                            dto.raw.get("builtin_job_url"))
                ats_info = {}
            with tx(conn):
                company_id = None
                if ats_info and dto.company_name != "Unknown (builtin)":
                    existing = conn.execute("SELECT id FROM companies WHERE name = ?",
                                            (dto.company_name,)).fetchone()
                    if not existing:
                        stats["companies_discovered"] += 1
                    company_id = get_or_create_company(
                        conn, dto.company_name, ats=ats_info["ats"],
                        board_token=ats_info["board_token"],
                        notes="auto-discovered via builtin")
                _, is_new = upsert_posting(conn, dto, company_id=company_id)
                stats["postings"] += 1
                stats["new"] += int(is_new)
        with tx(conn):
            log_event(conn, "builtin_search_polled",
                      payload={"name": search.get("name", search["url"])})
    with tx(conn):
        log_event(conn, "source_polled",
                  payload={"source": "builtin", "postings": stats["postings"],
                           "new": stats["new"], "errors": stats["errors"]})
    return stats


def close_stale(conn) -> int:
    """Mark postings not seen in the last 2 poll runs as closed."""
    row = conn.execute(
        "SELECT created_at FROM events WHERE event_type='heartbeat' "
        "AND json_extract(payload_json,'$.job')='poll' ORDER BY id DESC LIMIT 1 OFFSET 1"
    ).fetchone()
    if not row:
        return 0
    with tx(conn):
        cur = conn.execute(
            "UPDATE postings SET closed_at = datetime('now') "
            "WHERE closed_at IS NULL AND last_seen_at < ?", (row["created_at"],))
    return cur.rowcount


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        sync_registry(conn, settings.companies_path)
        searches = load_searches(settings.searches_path)
        ats_stats = run_ats_pollers(conn)
        bi_stats = run_builtin_poller(conn, searches)
        agg_stats = run_aggregator_pollers(conn, searches)
        closed = close_stale(conn)
        from . import crosslink
        linked = crosslink.link_cross_source(conn)
        with tx(conn):
            log_event(conn, "poll_run", payload={"ats": ats_stats, "builtin": bi_stats,
                                                 "aggregators": agg_stats, "closed": closed,
                                                 "crosslinked": linked})
        heartbeat(conn, "poll", ok=True,
                  detail=f"ats={ats_stats} builtin={bi_stats} agg={agg_stats} "
                         f"closed={closed} crosslinked={linked}")
        log.info("poll complete: ats=%s builtin=%s agg=%s closed=%d crosslinked=%d",
                 ats_stats, bi_stats, agg_stats, closed, linked)
    except Exception as e:
        heartbeat(conn, "poll", ok=False, detail=str(e))
        raise
    finally:
        conn.close()



if __name__ == "__main__":
    main()
