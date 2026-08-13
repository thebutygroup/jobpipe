"""The daily review email must show what's NEW since the previous email,
when each match happened, and recent matcher history — not the same
score-sorted list every morning."""

from jobpipe import publish
from jobpipe.db import log_event, tx, upsert_posting
from jobpipe.models import PostingDTO


def _seed_pending(conn, company, score, matched_at):
    conn.execute("INSERT OR IGNORE INTO applicants (id, name, user_ref,"
                 " profile_path) VALUES (1, 'Joe', 'joebuty', 'p')")
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name=company, source="ats", external_id=company, title="SDE",
        location="London", apply_url=f"https://x/{company}",
        description_text="d"))
    conn.execute("INSERT INTO matches (posting_id, applicant_id, score,"
                 " reasons_json, model, tokens_used, created_at)"
                 " VALUES (?,1,?,'[]','m',10,?)", (pid, score, matched_at))
    conn.execute("INSERT INTO applications (posting_id, applicant_id, state,"
                 " created_at, updated_at) VALUES (?,1,'PENDING_REVIEW',?,?)",
                 (pid, matched_at, matched_at))
    conn.commit()


def _publish_beat(conn, at):
    with tx(conn):
        log_event(conn, "heartbeat",
                  payload={"job": "publish", "ok": True, "detail": ""})
        conn.execute("UPDATE events SET created_at = ? WHERE id ="
                     " (SELECT MAX(id) FROM events)", (at,))
    conn.commit()


def test_new_matches_are_separated_and_dated(conn):
    _seed_pending(conn, "OldCo", 9, "2026-08-10T06:50:00")
    _seed_pending(conn, "FreshCo", 8, "2026-08-13T06:50:00")
    _publish_beat(conn, "2026-08-12T08:00:00")  # yesterday's email
    n, n_new, html, text = publish.build_digest(conn)
    assert n == 2 and n_new == 1
    assert "New since the last email (1)" in html and "FreshCo" in html
    assert "Still waiting (1)" in html and "OldCo" in html
    assert "matched 2026-08-13" in html and "matched 2026-08-10" in html
    assert text.splitlines()[1].startswith("- NEW FreshCo")


def test_no_prior_email_means_everything_is_new(conn):
    _seed_pending(conn, "OnlyCo", 9, "2026-08-13T06:50:00")
    n, n_new, html, _text = publish.build_digest(conn)
    assert n == 1 and n_new == 1
    assert "New since the last email (1)" in html


def test_quiet_day_says_nothing_new_and_shows_history(conn):
    _seed_pending(conn, "OldCo", 9, "2026-08-10T06:50:00")
    _publish_beat(conn, "2026-08-12T08:00:00")
    # some matcher history inside the 7-day window (score 3 = below threshold)
    conn.execute("UPDATE matches SET created_at = datetime('now','-1 day')")
    conn.execute("INSERT INTO matches (posting_id, applicant_id, score,"
                 " reasons_json, model, tokens_used, created_at) VALUES"
                 " ((SELECT MIN(id) FROM postings), 1, 3, '[]', 'm', 10,"
                 "  datetime('now'))")
    conn.commit()
    _publish_beat(conn, "9999-12-31T00:00:00")  # cutoff after every match
    n, n_new, html, text = publish.build_digest(conn)
    assert n_new == 0
    assert "nothing new since the last email" in html.lower()
    assert "Matcher activity, last 7 days" in html
    assert "matches / scored" in text


def test_activity_math(conn):
    for i, (score, at) in enumerate([(9, "now"), (3, "now"),
                                     (8, "now', '-2 days")]):
        conn.execute("INSERT OR IGNORE INTO applicants (id, name, user_ref,"
                     " profile_path) VALUES (1,'Joe','joebuty','p')")
        pid, _ = upsert_posting(conn, PostingDTO(
            company_name=f"C{i}", source="ats", external_id=f"c{i}",
            title="SDE", location="L", apply_url=f"https://x/c{i}"))
        conn.execute("INSERT INTO matches (posting_id, applicant_id, score,"
                     " reasons_json, model, tokens_used, created_at)"
                     f" VALUES (?,1,?,'[]','m',10,datetime('{at}'))",
                     (pid, score))
    conn.commit()
    days = {a["d"]: (a["hits"], a["scored"]) for a in publish.recent_activity(conn)}
    import datetime as dt
    today = dt.date.today().isoformat()
    assert days[today] == (1, 2)  # one >=7, two scored
