"""Send (or resend) the "your matches are ready" email.

  docker compose exec jobpipe-web python scripts/send_matches_email.py --user eeezee
  docker compose exec jobpipe-web python scripts/send_matches_email.py --all
  ... --user eeezee --dry-run     # show what would be sent, send nothing
  ... --user eeezee --force       # resend even if already sent

--all covers every ACTIVE applicant who has an email and hasn't received one
yet — safe to run repeatedly; the already-sent guard makes it idempotent.
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe import matches_mail  # noqa: E402
from jobpipe.db import connect  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    who = ap.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", help="user_ref of one applicant")
    who.add_argument("--all", action="store_true",
                     help="every active applicant not yet emailed")
    ap.add_argument("--limit", type=int, default=5, help="matches shown inline")
    ap.add_argument("--force", action="store_true", help="resend even if sent")
    ap.add_argument("--dry-run", action="store_true", help="compose, don't send")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.user:
            rows = conn.execute("SELECT * FROM applicants WHERE user_ref = ?",
                                (args.user,)).fetchall()
            if not rows:
                raise SystemExit(f"no applicant with user_ref {args.user!r}")
        else:
            rows = conn.execute(
                "SELECT * FROM applicants WHERE active = 1 ORDER BY id").fetchall()
        for row in rows:
            if args.dry_run:
                matches = matches_mail.top_matches(conn, row["id"], args.limit)
                if not matches:
                    print(f"{row['user_ref']}: no matches at threshold — would skip")
                    continue
                subject, _, text = matches_mail.compose(
                    row["user_ref"] or "", matches, len(matches))
                sent = matches_mail.already_sent(conn, row["user_ref"] or "")
                print(f"--- {row['user_ref']} (already_sent={sent}) ---")
                print(f"Subject: {subject}\n{text}\n")
            else:
                print(matches_mail.send_matches_ready(
                    conn, row, limit=args.limit, force=args.force))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
