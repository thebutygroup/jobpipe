"""Print each applicant's user_ref (the token for /job_matches/<ref>), backfilling
any missing token. Run:
  docker compose run --rm jobpipe-web python scripts/show_user_ref.py
"""
from __future__ import annotations

import secrets
import sys

sys.path.insert(0, ".")
from jobpipe.db import connect  # noqa: E402


def main() -> int:
    conn = connect()
    rows = conn.execute("SELECT id, name, user_ref FROM applicants").fetchall()
    if not rows:
        print("no applicants yet — run the matcher once to create one")
        return 0
    for r in rows:
        ref = r["user_ref"]
        if not ref:
            ref = secrets.token_urlsafe(8)
            conn.execute("UPDATE applicants SET user_ref = ? WHERE id = ?", (ref, r["id"]))
            conn.commit()
            print(f"{r['name']}: (generated) {ref}")
        else:
            print(f"{r['name']}: {ref}")
    print("\nMatch pages:  <DASHBOARD_BASE_URL>/job_matches/<ref>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
