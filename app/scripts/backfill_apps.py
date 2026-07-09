"""Create applications for existing matches that fell below the old threshold.

Used after lowering MATCH_THRESHOLD: matches are already scored (no LLM cost),
they just never got an application row. This backfills them into MATCHED state
so they render on the matches pages. Idempotent.
"""
import sys

sys.path.insert(0, "/app")

from jobpipe.config import settings  # noqa: E402
from jobpipe.db import connect, log_event, now, tx  # noqa: E402


def main() -> None:
    conn = connect()
    rows = conn.execute(
        "SELECT m.posting_id, m.applicant_id, m.score FROM matches m "
        "WHERE m.score >= ? AND NOT EXISTS (SELECT 1 FROM applications a "
        "WHERE a.posting_id = m.posting_id AND a.applicant_id = m.applicant_id)",
        (settings.match_threshold,)).fetchall()
    with tx(conn):
        for r in rows:
            conn.execute(
                "INSERT INTO applications (posting_id, applicant_id, state,"
                " created_at, updated_at) VALUES (?,?,'MATCHED',?,?)",
                (r["posting_id"], r["applicant_id"], now(), now()))
            log_event(conn, "transition:MATCHED", posting_id=r["posting_id"],
                      payload={"backfill": True, "score": r["score"]})
    print(f"backfilled {len(rows)} applications at threshold {settings.match_threshold}")


if __name__ == "__main__":
    main()
