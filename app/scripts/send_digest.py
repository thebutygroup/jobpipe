"""Send the weekly "new since last time" digest by hand.

  docker compose exec jobpipe-web python scripts/send_digest.py --dry-run
  docker compose exec jobpipe-web python scripts/send_digest.py
  ... --user eeezee              # one applicant only
  ... --user eeezee --dry-run

--dry-run composes everything and sends nothing, so you can see exactly who
would be mailed and why before letting the scheduler do it on Mondays.

The same guards apply here as in the scheduled run: confirmed address, not
shadow-banned, not opted out, and NOTHING NEW MEANS NO EMAIL.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe import digest  # noqa: E402
from jobpipe.db import connect  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", help="user_ref of one applicant (default: everyone eligible)")
    ap.add_argument("--dry-run", action="store_true",
                    help="compose and report, send nothing")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.user:
            row = conn.execute("SELECT * FROM applicants WHERE user_ref = ?",
                               (args.user,)).fetchone()
            if row is None:
                sys.exit(f"no applicant with user_ref {args.user!r}")
            print(digest.send_one(conn, dict(row), dry_run=args.dry_run))
            return
        eligible = digest.recipients(conn)
        print(f"{len(eligible)} eligible recipient(s)"
              f"{' — DRY RUN, nothing will send' if args.dry_run else ''}")
        for row in eligible:
            print("  " + digest.send_one(conn, row, dry_run=args.dry_run))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
