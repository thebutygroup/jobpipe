"""Weekly index scan runner.

1. Refresh constituents (Wikipedia + static fallback) -> upsert index_companies.
2. Resolve a capped batch of 'new' companies (careers URL + classification).
   - resolved_ats  -> auto-insert into the main registry (daily pollers take over)
   - workday       -> stored for step 3
   - bespoke       -> flagged for the v2 agent, never scraped
3. Poll every known Workday tenant with the profile's target titles.
"""

from __future__ import annotations

import logging

from ..config import settings
from ..db import connect, get_or_create_company, heartbeat, log_event, now, tx, upsert_posting
from ..profile import load_profile
from . import workday
from .constituents import merged_constituents
from .resolver import resolve_company

log = logging.getLogger(__name__)


def sync_constituents(conn) -> dict:
    lists = merged_constituents(settings.constituents_static_path)
    stats = {idx: len(names) for idx, names in lists.items()}
    with tx(conn):
        for idx, names in lists.items():
            for name in names:
                conn.execute(
                    "INSERT INTO index_companies (name, idx) VALUES (?, ?) "
                    "ON CONFLICT(name) DO NOTHING", (name, idx))
    return stats


def enqueue_dream_companies(conn) -> int:
    """Manual dream-company list (dream.yaml): [{name, website?}, ...].
    Website, when given, becomes the resolution domain directly."""
    import os

    import yaml
    if not os.path.exists(settings.dream_path):
        return 0
    with open(settings.dream_path) as f:
        entries = yaml.safe_load(f) or []
    added = 0
    with tx(conn):
        for e in entries:
            name = (e.get("name") or "").strip()
            if not name:
                continue
            domain = (e.get("website") or "").replace("https://", "").replace(
                "http://", "").split("/")[0].removeprefix("www.")
            cur = conn.execute(
                "INSERT INTO index_companies (name, idx, domain) VALUES (?, 'dream', ?) "
                "ON CONFLICT(name) DO NOTHING", (name, domain))
            added += cur.rowcount
    return added


def enqueue_board_companies(conn) -> int:
    """Feed the resolver queue from job boards: companies whose postings we
    discovered on Built In but who have no ATS in the registry yet. Their
    Built In company page URL becomes the website hint (source_url), so the
    resolver can skip Wikipedia and read the 'View Website' link directly."""
    rows = conn.execute(
        "SELECT DISTINCT c.name, "
        "       json_extract(p.raw_json, '$.builtin_company_url') AS company_url "
        "FROM postings p JOIN companies c ON c.id = p.company_id "
        "WHERE p.source = 'builtin' AND p.closed_at IS NULL "
        "AND (c.ats IS NULL OR c.ats = '' OR c.ats = 'builtin') "
        "AND c.name != 'Unknown (builtin)' "
        "AND NOT EXISTS (SELECT 1 FROM index_companies i WHERE i.name = c.name)"
    ).fetchall()
    added = 0
    with tx(conn):
        for row in rows:
            conn.execute(
                "INSERT INTO index_companies (name, idx, source_url) VALUES (?, 'builtin', ?) "
                "ON CONFLICT(name) DO NOTHING", (row["name"], row["company_url"]))
            added += 1
    return added


def resolve_batch(conn, cap: int) -> dict:
    rows = conn.execute("SELECT id, name, source_url, domain, careers_url FROM index_companies WHERE status = 'new' "
                        "ORDER BY id LIMIT ?", (cap,)).fetchall()
    stats = {"resolved_ats": 0, "workday": 0, "bespoke": 0, "unresolved": 0}
    for row in rows:
        try:
            r = resolve_company(row["name"], domain=row["domain"] or "",
                                source_url=row["source_url"] or "",
                                careers_url=row["careers_url"] or "")
        except Exception:
            log.exception("resolution crashed for %s", row["name"])
            r = {"status": "unresolved", "notes": "resolver error"}
        with tx(conn):
            conn.execute(
                "UPDATE index_companies SET status=?, domain=?, careers_url=?, ats=?,"
                " board_token=?, workday_host=?, workday_tenant=?, workday_site=?,"
                " notes=?, last_checked=? WHERE id=?",
                (r.get("status"), r.get("domain"), r.get("careers_url"), r.get("ats"),
                 r.get("board_token"), r.get("workday_host"), r.get("workday_tenant"),
                 r.get("workday_site"), r.get("notes"), now(), row["id"]))
            if r.get("status") == "resolved_ats":
                get_or_create_company(conn, row["name"], ats=r["ats"],
                                      board_token=r["board_token"],
                                      notes="auto-discovered via index scan")
        stats[r.get("status", "unresolved")] = stats.get(r.get("status", "unresolved"), 0) + 1
    return stats


def poll_workday(conn) -> dict:
    profile = load_profile(settings.profile_path)
    terms = profile.preferences.target_titles
    rows = conn.execute("SELECT name, workday_host, workday_tenant, workday_site "
                        "FROM index_companies WHERE status = 'workday' "
                        "AND workday_host IS NOT NULL").fetchall()
    stats = {"tenants": 0, "postings": 0, "new": 0}
    for row in rows:
        stats["tenants"] += 1
        dtos = workday.fetch(row["name"], row["workday_host"], row["workday_tenant"],
                             row["workday_site"] or "External", terms)
        with tx(conn):
            for dto in dtos:
                _, is_new = upsert_posting(conn, dto)
                stats["postings"] += 1
                stats["new"] += int(is_new)
    return stats


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    conn = connect()
    try:
        c = sync_constituents(conn)
        d = enqueue_dream_companies(conn)
        b = enqueue_board_companies(conn)
        r = resolve_batch(conn, settings.indexscan_resolve_cap)
        w = poll_workday(conn)
        with tx(conn):
            log_event(conn, "indexscan_run", payload={"constituents": c, "dream": d, "board_enqueued": b, "resolved": r,
                                                      "workday": w})
        heartbeat(conn, "indexscan", ok=True, detail=f"c={c} r={r} w={w}")
        log.info("indexscan complete: constituents=%s dream=%s board=%s resolved=%s workday=%s",
                 c, d, b, r, w)
        from ..crawler import runner as crawler_runner
        crawler_runner.main()
    except Exception as e:
        heartbeat(conn, "indexscan", ok=False, detail=str(e))
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
