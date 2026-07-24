"""Remove test accounts and refund their daily signup-activation slots.

Repeatable by design: sign up test users with the `testrun` prefix
(testrun1, testrun-maya, ...), then run this to wipe them and their
counter events. Real users' activation slots are untouched — only events
attributable to the prefix are deleted.

Usage (from C:\\stack on the home box):
    docker compose exec jobpipe-web python scripts/cleanup_testruns.py
    docker compose exec jobpipe-web python scripts/cleanup_testruns.py --prefix demo
"""

from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from jobpipe.db import connect  # noqa: E402


def purge(conn, prefix: str = "testrun") -> dict:
    like = f"{prefix}%"
    refs = [r["user_ref"] for r in conn.execute(
        "SELECT user_ref FROM applicants WHERE user_ref LIKE ?", (like,))]
    for ref in refs:
        aid = conn.execute("SELECT id FROM applicants WHERE user_ref = ?",
                           (ref,)).fetchone()["id"]
        conn.execute("DELETE FROM outcomes WHERE application_id IN "
                     "(SELECT id FROM applications WHERE applicant_id = ?)", (aid,))
        conn.execute("DELETE FROM events WHERE application_id IN "
                     "(SELECT id FROM applications WHERE applicant_id = ?)", (aid,))
        conn.execute("DELETE FROM applications WHERE applicant_id = ?", (aid,))
        conn.execute("DELETE FROM matches WHERE applicant_id = ?", (aid,))
        conn.execute("DELETE FROM applicants WHERE id = ?", (aid,))
    # Refund ONLY the prefix's own activation/cap events — real signups today
    # keep counting against the cap.
    slots = conn.execute(
        "DELETE FROM events WHERE event_type IN "
        "('signup_auto_activated', 'signup_capped', 'signup_instant_match') "
        "AND json_extract(payload_json, '$.user_ref') LIKE ?", (like,)).rowcount
    conn.commit()
    return {"users_removed": refs, "counter_events_cleared": slots}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix", default="testrun",
                    help="user_ref prefix to purge (default: testrun)")
    args = ap.parse_args()
    conn = connect()
    try:
        result = purge(conn, args.prefix)
        print(f"removed {len(result['users_removed'])} users: "
              f"{result['users_removed']}")
        print(f"cleared {result['counter_events_cleared']} signup-counter events")
        print("remaining:", [dict(r) for r in conn.execute(
            "SELECT id, name, user_ref, active FROM applicants")])
    finally:
        conn.close()


if __name__ == "__main__":
    main()
