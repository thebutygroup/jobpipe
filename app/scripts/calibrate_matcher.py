"""Is Haiku's match scoring trustworthy? Settle it with data, not vibes.

Samples already-scored postings for one user (stratified across score bands
so lows and highs are both represented), re-scores each with a stronger judge
model, and prints the disagreement:

  docker compose exec jobpipe-scheduler python scripts/calibrate_matcher.py \
      --user joebuty --n 20
  ... [--judge claude-sonnet-4-6]

Reads the verdict like this:
- mean |delta| <= 1 and few threshold flips  => Haiku is fine, keep it
- systematic bias (judge always higher/lower) => adjust MATCH_THRESHOLD
- big scatter / many flips                    => set MATCH_MODEL to the judge
Costs: ~n Sonnet calls (pennies).
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe.config import settings  # noqa: E402
from jobpipe.db import connect  # noqa: E402
from jobpipe.matching.matcher import call_model  # noqa: E402
from jobpipe.profile import load_applicant_profile  # noqa: E402

BANDS = ((0, 3), (4, 6), (7, 10))


def sample(conn, applicant_id: int, n: int) -> list:
    """Up to n postings, stratified across score bands, newest scores win."""
    rows = []
    per_band = max(1, n // len(BANDS))
    for lo, hi in BANDS:
        rows += conn.execute(
            "SELECT p.id, p.title, p.location, p.description_text,"
            "       c.name AS company_name, MAX(m.score) AS haiku"
            " FROM matches m JOIN postings p ON p.id = m.posting_id"
            " JOIN companies c ON c.id = p.company_id"
            " WHERE m.applicant_id = ? AND m.score BETWEEN ? AND ?"
            " AND p.closed_at IS NULL"
            " GROUP BY p.id ORDER BY RANDOM() LIMIT ?",
            (applicant_id, lo, hi, per_band)).fetchall()
    return rows


def agreement_stats(pairs: list[tuple[int, int]],
                    threshold: int) -> dict:
    """pairs = [(haiku, judge), ...]"""
    if not pairs:
        return {"n": 0}
    deltas = [j - h for h, j in pairs]
    flips = sum(1 for h, j in pairs
                if (h >= threshold) != (j >= threshold))
    return {
        "n": len(pairs),
        "mean_delta": round(sum(deltas) / len(deltas), 2),   # + => judge higher
        "mean_abs_delta": round(sum(abs(d) for d in deltas) / len(deltas), 2),
        "within_1": sum(1 for d in deltas if abs(d) <= 1),
        "threshold_flips": flips,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--judge", default="claude-sonnet-4-6")
    args = ap.parse_args()

    import anthropic
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM applicants WHERE user_ref = ?",
                           (args.user,)).fetchone()
        if not row:
            raise SystemExit(f"no applicant {args.user!r}")
        profile = load_applicant_profile(row)
        raw_yaml = row["profile_yaml"] or ""
        postings = sample(conn, row["id"], args.n)
        if not postings:
            raise SystemExit("nothing scored yet for this user — run the matcher first")
        print(f"re-scoring {len(postings)} postings with {args.judge} "
              f"(haiku = stored {settings.match_model} score)\n")
        print(f"{'haiku':>5} {'judge':>5} {'diff':>5}  posting")
        pairs = []
        for p in postings:
            try:
                result, _tokens = call_model(client, profile, p, raw_yaml,
                                             model=args.judge)
            except Exception as e:  # noqa: BLE001 - keep going, report
                print(f"{p['haiku']:>5} {'ERR':>5} {'':>5}  {p['company_name']}"
                      f" — {p['title'][:55]}  ({e})")
                continue
            diff = result.score - p["haiku"]
            pairs.append((p["haiku"], result.score))
            flag = "  <-- crosses threshold" if (
                (p["haiku"] >= settings.match_threshold)
                != (result.score >= settings.match_threshold)) else ""
            print(f"{p['haiku']:>5} {result.score:>5} {diff:>+5}  "
                  f"{p['company_name']} — {p['title'][:55]}{flag}")
        s = agreement_stats(pairs, settings.match_threshold)
        print(f"\nagreement over {s['n']} postings:")
        print(f"  mean delta (judge - haiku): {s['mean_delta']:+}  "
              f"(positive = {args.judge} scores higher)")
        print(f"  mean |delta|: {s['mean_abs_delta']}   "
              f"within ±1: {s['within_1']}/{s['n']}")
        print(f"  verdicts flipped across threshold {settings.match_threshold}: "
              f"{s['threshold_flips']}")
        print("\nrules of thumb: |delta|<=1 & few flips => Haiku fine. "
              "Consistent bias => adjust MATCH_THRESHOLD. "
              "Scatter/flips => MATCH_MODEL=" + args.judge)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
