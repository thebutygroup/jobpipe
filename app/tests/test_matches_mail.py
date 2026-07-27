"""The 'your matches are ready' follow-up email: composed from real matches,
sent once, recorded in the DB."""

import yaml

from jobpipe import matches_mail
from jobpipe.db import upsert_posting
from jobpipe.models import PostingDTO


def seed_user(conn, email="user@example.com", n_matches=3):
    profile_yaml = yaml.safe_dump({
        "identity": {"full_name": "Test User", "email": email,
                     "location": "London"},
        "preferences": {"target_titles": ["Data Engineer"],
                        "locations_ok": ["London"]},
    })
    conn.execute("INSERT INTO applicants (name, user_ref, profile_path,"
                 " profile_yaml, active) VALUES ('Test User','tuser','',?,1)",
                 (profile_yaml,))
    aid = conn.execute("SELECT id FROM applicants WHERE user_ref='tuser'"
                       ).fetchone()["id"]
    for i in range(n_matches):
        pid, _ = upsert_posting(conn, PostingDTO(
            company_name=f"Co{i}", source="ats", external_id=f"e{i}",
            title=f"Data Engineer {i}", location="London",
            apply_url=f"https://boards.greenhouse.io/co{i}/{i}",
            description_text="d"))
        conn.execute("INSERT INTO matches (posting_id, applicant_id, score,"
                     " reasons_json, model, tokens_used, created_at)"
                     " VALUES (?,?,?,'[]','m',10,datetime('now'))",
                     (pid, aid, 9 - i))
    conn.commit()
    return conn.execute("SELECT * FROM applicants WHERE id=?", (aid,)).fetchone()


def test_compose_lists_top_matches(conn):
    row = seed_user(conn)
    matches = matches_mail.top_matches(conn, row["id"])
    subject, html, text = matches_mail.compose("tuser", matches, len(matches))
    assert "3" in subject
    assert "Co0" in html and "9/10" in html and "/job_matches/tuser" in html
    assert "Co0" in text


def test_send_records_event_and_wont_double_send(conn, monkeypatch):
    row = seed_user(conn)
    sent = []
    from jobpipe import notify
    monkeypatch.setattr(notify, "send_email",
                        lambda **kw: sent.append(kw) or True)
    out = matches_mail.send_matches_ready(conn, row)
    assert "sent" in out and len(sent) == 1
    assert sent[0]["to"] == "user@example.com"
    ev = conn.execute("SELECT payload_json FROM events WHERE"
                      " event_type='signup_email'").fetchone()
    assert '"matches_ready"' in ev["payload_json"] and '"ok": true' in ev["payload_json"]
    # second call: guarded
    out2 = matches_mail.send_matches_ready(conn, row)
    assert "already sent" in out2 and len(sent) == 1
    # force overrides
    out3 = matches_mail.send_matches_ready(conn, row, force=True)
    assert "sent" in out3 and len(sent) == 2


def test_no_email_and_no_matches_are_graceful(conn, monkeypatch):
    row = seed_user(conn, email="", n_matches=0)
    out = matches_mail.send_matches_ready(conn, row)
    assert "no email" in out
    row2_yaml = row["profile_yaml"].replace('email: ""', "email: a@b.com")
    conn.execute("UPDATE applicants SET profile_yaml=? WHERE id=?",
                 (row2_yaml.replace("email: ''", "email: a@b.com"), row["id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM applicants WHERE id=?", (row["id"],)).fetchone()
    out = matches_mail.send_matches_ready(conn, row)
    assert "no matches" in out
