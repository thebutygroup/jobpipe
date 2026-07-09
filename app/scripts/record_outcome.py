"""Manually record an outcome (or link an unlinked one) from the CLI.

  python scripts/record_outcome.py <application_id> <outcome_type>
  python scripts/record_outcome.py --link <outcome_id> <application_id>

outcome_type: rejected | interview_invite | assessment | offer | withdrawn | other
"""
import sys

sys.path.insert(0, "/app")

from jobpipe.db import connect, log_event, now, tx  # noqa: E402

TYPES = {"rejected", "interview_invite", "assessment", "offer", "withdrawn", "other"}


def main() -> None:
    conn = connect()
    if sys.argv[1] == "--link":
        outcome_id, app_id = int(sys.argv[2]), int(sys.argv[3])
        with tx(conn):
            conn.execute("UPDATE outcomes SET application_id = ? WHERE id = ?",
                         (app_id, outcome_id))
        print(f"outcome {outcome_id} linked to application {app_id}")
        return
    app_id, outcome = int(sys.argv[1]), sys.argv[2]
    assert outcome in TYPES, f"outcome must be one of {sorted(TYPES)}"
    with tx(conn):
        conn.execute(
            "INSERT INTO outcomes (application_id, outcome_type, occurred_at,"
            " source, created_at) VALUES (?,?,?,?,?)",
            (app_id, outcome, now(), "manual", now()))
        log_event(conn, f"outcome:{outcome}", application_id=app_id,
                  payload={"source": "manual"})
    print(f"recorded {outcome} for application {app_id}")


if __name__ == "__main__":
    main()
