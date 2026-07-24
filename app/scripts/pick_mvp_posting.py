"""Find the MVP application target: the best match whose route resolves to a
friendly platform (Greenhouse first). Live network — run on the box:

  docker compose exec jobpipe-web python scripts/pick_mvp_posting.py --user joebuty

Resolves and PERSISTS routes for the user's top matches (so this doubles as
a route-coverage report), then names the winner.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe.apply.routes import BROWSER_FORM, ensure_route  # noqa: E402
from jobpipe.db import connect  # noqa: E402

PREFERRED = ("greenhouse", "lever")   # friendliest first


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--limit", type=int, default=15, help="matches to route-resolve")
    args = ap.parse_args()
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT a.id AS app_id, p.id AS posting_id, c.name AS company,"
            "       p.title, m.score"
            " FROM applications a"
            " JOIN postings p ON p.id = a.posting_id"
            " JOIN companies c ON c.id = p.company_id"
            " JOIN applicants ap ON ap.id = a.applicant_id"
            " LEFT JOIN matches m ON m.posting_id = a.posting_id"
            "      AND m.applicant_id = a.applicant_id"
            " WHERE ap.user_ref = ? AND a.state IN"
            "       ('MATCHED','PREPARED','PENDING_REVIEW')"
            " AND p.closed_at IS NULL"
            " ORDER BY m.score DESC, a.created_at DESC LIMIT ?",
            (args.user, args.limit)).fetchall()
        if not rows:
            raise SystemExit(f"no reviewable matches for {args.user!r}")
        candidates = []
        print(f"route-resolving top {len(rows)} matches for {args.user} "
              f"(closed postings excluded):\n")
        for r in rows:
            try:
                route = ensure_route(conn, r["posting_id"])
            except Exception as e:  # noqa: BLE001 - report and continue
                print(f"  [{r['score']}] {r['company']} — {r['title'][:50]}"
                      f"  !! resolution failed: {e}")
                continue
            print(f"  [{r['score']}] {r['company']} — {r['title'][:50]}")
            print(f"       -> {route.platform} ({route.method}) {route.final_url[:70]}")
            if route.method == BROWSER_FORM and route.platform in PREFERRED:
                candidates.append((r, route))
        print()
        if candidates:
            print(f"{len(candidates)} candidate(s) on preferred platforms "
                  f"(a dry run that finds the posting closed marks it and you "
                  f"re-pick):")
            for r, route in candidates:
                print(f"  application {r['app_id']} — {r['company']} / "
                      f"{r['title'][:50]} on {route.platform}")
            r, route = candidates[0]
            print(f"\nMVP TARGET: application {r['app_id']} — {r['company']} / "
                  f"{r['title']} on {route.platform}")
            print(f"next: python scripts/apply_dry_run.py --app-id {r['app_id']}")
        else:
            print("No live match resolved to a preferred platform "
                  "(greenhouse/lever). Options: raise --limit, or run the MVP "
                  "against a company_site route with the generic applier.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
