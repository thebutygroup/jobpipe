"""Ingest the toy fixture payloads through the full adapter + dedupe path.

Lets you exercise /sources, /all and the whole pipeline before the Adzuna /
Reed API keys exist. The fixture jobs are deliberately unreal (Trainee
Preschooler, Junior Tooth Fairy) so they can't be mistaken for live data.

Usage (from app/):  python scripts/demo_ingest.py [--db PATH]
Re-running is idempotent — that's half the point.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe.db import connect, log_event, tx, upsert_posting  # noqa: E402
from jobpipe.sources import registry  # noqa: E402
from jobpipe.sources.base import SearchSpec  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="override DB path (default: settings.db_path)")
    args = ap.parse_args()
    conn = connect(args.db)
    spec = SearchSpec(source="demo", name="demo-fixtures", keywords="toy jobs",
                      location="London")
    totals = {}
    try:
        for source, fname in (("adzuna", "adzuna_search.json"),
                              ("reed", "reed_search.json")):
            payload = json.loads((FIXTURES / fname).read_text())
            adapter = registry.get(source)
            st = totals[source] = {"postings": 0, "new": 0, "errors": 0}
            with tx(conn):
                for raw in payload["results"]:
                    dto = adapter.normalize(raw, spec)
                    dto.source_detail = dto.source_detail or source
                    _, is_new = upsert_posting(conn, dto)
                    st["postings"] += 1
                    st["new"] += int(is_new)
                log_event(conn, "source_polled", payload={"source": source, **st,
                                                          "demo_fixture": True})
        n_jobs = conn.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"]
        n_sight = conn.execute("SELECT COUNT(*) c FROM source_postings").fetchone()["c"]
        print(f"ingested: {totals}")
        print(f"db now: {n_jobs} canonical postings, {n_sight} sightings "
              f"-> open /sources to see uniqueness + overlap")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
