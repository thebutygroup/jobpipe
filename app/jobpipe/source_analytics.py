"""Source analytics: which sources are worth having?

Everything derives at read time from source_postings (provenance) joined to
postings/matches — no counters to drift. A "job" here is a canonical posting;
a "sighting" is one source seeing that job.

Definitions:
- unique jobs of source S: jobs whose ONLY sighting is from S
- overlap(A, B): fraction of A's jobs that B also saw (row-normalised, so the
  matrix is asymmetric — a big source can cover a small one without the
  reverse)
- first-seen wins: S saw the job strictly before every other source did
- match quality: score distribution of matches on jobs S saw
"""

from __future__ import annotations

from itertools import combinations


def _job_sources(conn) -> dict[int, set[str]]:
    """posting_id -> set of sources that sighted it."""
    out: dict[int, set[str]] = {}
    for r in conn.execute("SELECT DISTINCT posting_id, source FROM source_postings"):
        out.setdefault(r["posting_id"], set()).add(r["source"])
    return out


def source_summary(conn) -> list[dict]:
    """Per-source: volume, uniqueness, first-seen wins, match quality."""
    jobs = _job_sources(conn)
    firsts = dict(conn.execute(
        "SELECT posting_id, source FROM ("
        "  SELECT posting_id, source, first_seen_at, "
        "         MIN(first_seen_at) OVER (PARTITION BY posting_id) AS earliest, "
        "         COUNT(*) OVER (PARTITION BY posting_id) AS n "
        "  FROM source_postings) "
        "WHERE first_seen_at = earliest AND n > 1").fetchall())
    scores: dict[str, list[int]] = {}
    for r in conn.execute(
            "SELECT sp.source, m.score FROM matches m "
            "JOIN source_postings sp ON sp.posting_id = m.posting_id"):
        scores.setdefault(r["source"], []).append(r["score"])

    stats: dict[str, dict] = {}
    for pid, sources in jobs.items():
        for s in sources:
            st = stats.setdefault(s, {"jobs": 0, "unique": 0, "shared": 0, "first": 0})
            st["jobs"] += 1
            if len(sources) == 1:
                st["unique"] += 1
            else:
                st["shared"] += 1
    for pid, winner in firsts.items():
        if winner in stats:
            stats[winner]["first"] += 1

    out = []
    for source, st in sorted(stats.items(), key=lambda kv: -kv[1]["jobs"]):
        ss = sorted(scores.get(source, []))
        out.append({
            "source": source,
            "jobs": st["jobs"],
            "unique": st["unique"],
            "uniqueness_pct": round(100 * st["unique"] / st["jobs"]) if st["jobs"] else 0,
            "duplicate_pct": round(100 * st["shared"] / st["jobs"]) if st["jobs"] else 0,
            "first_seen_wins": st["first"],
            "matches": len(ss),
            "avg_score": round(sum(ss) / len(ss), 1) if ss else None,
            "strong_matches": sum(1 for s in ss if s >= 7),
        })
    return out


def overlap_matrix(conn) -> dict:
    """{'sources': [...], 'rows': {A: {B: pct or None}}} — pct of A's jobs
    that B also saw. Diagonal is None."""
    jobs = _job_sources(conn)
    sources = sorted({s for ss in jobs.values() for s in ss})
    totals = {s: 0 for s in sources}
    both: dict[tuple[str, str], int] = {}
    for ss in jobs.values():
        for s in ss:
            totals[s] += 1
        for a, b in combinations(sorted(ss), 2):
            both[(a, b)] = both.get((a, b), 0) + 1
    rows: dict[str, dict] = {}
    for a in sources:
        rows[a] = {}
        for b in sources:
            if a == b or not totals[a]:
                rows[a][b] = None
                continue
            shared = both.get((a, b) if a < b else (b, a), 0)
            rows[a][b] = round(100 * shared / totals[a])
    return {"sources": sources, "rows": rows, "totals": totals}


def volume_by_day(conn, days: int = 30) -> dict:
    """{'days': [...], 'series': {source: [count per day]}} for the last N days."""
    rows = conn.execute(
        "SELECT source, date(first_seen_at) AS d, COUNT(*) AS n FROM source_postings "
        "WHERE first_seen_at >= date('now', ?) GROUP BY source, d",
        (f"-{days} days",)).fetchall()
    days_seen = sorted({r["d"] for r in rows})
    series: dict[str, list[int]] = {}
    for r in rows:
        series.setdefault(r["source"], [0] * len(days_seen))
    for r in rows:
        series[r["source"]][days_seen.index(r["d"])] = r["n"]
    return {"days": days_seen, "series": series}
