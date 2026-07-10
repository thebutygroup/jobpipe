"""Approve (or reject) new signups. New users onboard INACTIVE and consume
zero LLM tokens until approved here.

  python scripts/approve_user.py                 # list pending signups
  python scripts/approve_user.py approve <id>    # activate
  python scripts/approve_user.py reject <id>     # delete signup + profile
  python scripts/approve_user.py list-all        # everyone, with status
"""
import os
import sys

sys.path.insert(0, "/app")

from jobpipe.db import connect, log_event, tx  # noqa: E402


def show(rows) -> None:
    for r in rows:
        state = "ACTIVE" if r["active"] else "pending"
        preview = " ".join((r["profile_yaml"] or "")[:160].split())
        print(f"[{r['id']:>3}] {state:<8} {r['name']:<24} ref={r['user_ref']}")
        if preview:
            print(f"      {preview}...")


def main() -> None:
    conn = connect()
    args = sys.argv[1:]
    if not args:
        rows = conn.execute("SELECT * FROM applicants WHERE active = 0"
                            " ORDER BY id").fetchall()
        print(f"{len(rows)} pending signup(s):" if rows else "no pending signups")
        show(rows)
        return
    if args[0] == "list-all":
        show(conn.execute("SELECT * FROM applicants ORDER BY active DESC, id").fetchall())
        return
    action, uid = args[0], int(args[1])
    row = conn.execute("SELECT * FROM applicants WHERE id = ?", (uid,)).fetchone()
    if not row:
        raise SystemExit(f"no applicant {uid}")
    with tx(conn):
        if action == "approve":
            conn.execute("UPDATE applicants SET active = 1 WHERE id = ?", (uid,))
            log_event(conn, "user_approved", payload={"applicant_id": uid})
            base = os.environ.get("DASHBOARD_BASE_URL", "https://jobs.thebutygroup.com").rstrip("/")
            print(f"approved {row['name']} (id {uid}).")
            print(f"  1. run their first match:  python -m jobpipe.matching.matcher --applicant {uid}")
            print(f"  2. send them their link:   {base}/job_matches/{row['user_ref']}")
        elif action == "reject":
            conn.execute("DELETE FROM applicants WHERE id = ? AND active = 0", (uid,))
            log_event(conn, "user_rejected", payload={"applicant_id": uid})
            print(f"rejected and removed signup {row['name']} (id {uid})")
        else:
            raise SystemExit(f"unknown action {action}")


if __name__ == "__main__":
    main()
