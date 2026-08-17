"""The weekly digest.

The rule under test throughout (changed 17 Aug 2026): EVERY eligible user
hears from us on Monday — matches, near-misses, or an honest quiet-week note
with the widen-the-net dial. Never-confirmed signups get a capped reminder.
Opt-outs, shadow-bans and bounced addresses are still never mailed.
"""

import yaml

from jobpipe import digest
from jobpipe.db import now as db_now
from jobpipe.db import upsert_posting
from jobpipe.models import PostingDTO


def seed_user(conn, user_ref="tuser", email="user@example.com", **cols):
    profile_yaml = yaml.safe_dump({
        "identity": {"full_name": "Test User", "email": email, "location": "London"},
        "preferences": {"target_titles": ["Data Engineer"], "locations_ok": ["London"]},
    })
    fields = {"active": 1, "email_confirmed_at": "2026-01-01T00:00:00",
              "shadow_banned": 0, "digest_opt_out": 0, **cols}
    names = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT INTO applicants (name, user_ref, profile_path, profile_yaml, {names})"
        f" VALUES ('Test User', ?, '', ?, {marks})",
        (user_ref, profile_yaml, *fields.values()))
    conn.commit()
    return conn.execute("SELECT * FROM applicants WHERE user_ref = ?",
                        (user_ref,)).fetchone()


def add_match(conn, applicant_id, score=9, created_at=None, tag="a"):
    # Default to "just now": a first digest only reaches back
    # FIRST_DIGEST_WINDOW_DAYS, so a match dated last month is correctly
    # invisible to it.
    created_at = created_at or db_now()
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name=f"Co{tag}", source="ats", external_id=f"e{tag}",
        title=f"Data Engineer {tag}", location="London",
        apply_url=f"https://boards.greenhouse.io/co/{tag}", description_text="d"))
    conn.execute("INSERT INTO matches (posting_id, applicant_id, score,"
                 " reasons_json, model, tokens_used, created_at)"
                 " VALUES (?,?,?,'[]','m',10,?)", (pid, applicant_id, score, created_at))
    conn.commit()
    return pid


def _capture(monkeypatch):
    sent = []
    from jobpipe import notify
    monkeypatch.setattr(notify, "send_email", lambda **kw: sent.append(kw) or True)
    return sent


# ---- who is eligible at all -------------------------------------------------------

def test_recipients_excludes_unconfirmed_banned_and_opted_out(conn):
    seed_user(conn, "yes")
    seed_user(conn, "unconfirmed", email="b@x.com", email_confirmed_at=None)
    seed_user(conn, "banned", email="c@x.com", shadow_banned=1)
    seed_user(conn, "stopped", email="d@x.com", digest_opt_out=1)
    seed_user(conn, "inactive", email="e@x.com", active=0)
    assert [r["user_ref"] for r in digest.recipients(conn)] == ["yes"]


# ---- the core rule ----------------------------------------------------------------

def test_new_matches_are_sent_and_recorded(conn, monkeypatch):
    row = seed_user(conn)
    add_match(conn, row["id"], score=9)
    sent = _capture(monkeypatch)
    out = digest.send_one(conn, dict(row))
    assert "sent [matches]" in out
    assert len(sent) == 1 and sent[0]["to"] == "user@example.com"
    assert "Coa" in sent[0]["html_body"] and "9/10" in sent[0]["html_body"]
    ev = conn.execute(
        "SELECT payload_json FROM events WHERE event_type='signup_email'"
        " AND json_extract(payload_json,'$.kind')='digest'").fetchone()
    assert ev and '"ok": 1' in ev["payload_json"].replace("true", "1")


def test_no_matches_sends_the_quiet_week_note(conn, monkeypatch):
    """Joe's rule since 17 Aug: silence reads as 'the tool died' — a quiet
    week gets an honest note with the widen-the-net dial instead."""
    row = seed_user(conn)
    sent = _capture(monkeypatch)
    out = digest.send_one(conn, dict(row))
    assert "sent [quiet_week]" in out
    assert len(sent) == 1
    html = sent[0]["html_body"]
    assert "quiet" in sent[0]["subject"].lower()
    assert "job titles" in html and "/profile/tuser/" in html
    assert "Data Engineer" in html          # shows what we searched on
    for banned in ("lack", "missing", "gap"):
        assert banned not in html.lower()   # positive framing, always


def test_cutoff_is_the_last_email_not_a_fixed_window(conn, monkeypatch):
    """Anything the user was already told about must not come round again.

    Anchored on a matches-ready email so the weekly resend guard isn't what's
    doing the work here — this is the cutoff itself under test.
    """
    row = seed_user(conn)
    anchor = "2026-07-25T12:00:00"
    conn.execute(
        "INSERT INTO events (event_type, payload_json, created_at) VALUES"
        " ('signup_email', ?, ?)",
        ('{"kind": "matches_ready", "user_ref": "tuser", "ok": 1}', anchor))
    conn.commit()
    assert digest.cutoff_for(conn, "tuser") == anchor

    add_match(conn, row["id"], created_at="2026-07-24T00:00:00", tag="before")
    sent = _capture(monkeypatch)
    out = digest.send_one(conn, dict(row))
    # pre-cutoff match is invisible: this week is a quiet one for them
    assert "sent [quiet_week]" in out
    assert "Cobefore" not in sent[0]["html_body"]
    conn.execute("DELETE FROM events WHERE event_type='signup_email'"
                 " AND json_extract(payload_json,'$.kind')='digest'")
    conn.commit()                                # clear the resend guard

    add_match(conn, row["id"], created_at="2026-07-26T00:00:00", tag="after")
    assert "sent [matches]" in digest.send_one(conn, dict(row))
    assert len(sent) == 2
    assert "Coafter" in sent[1]["html_body"] and "Cobefore" not in sent[1]["html_body"]


def test_opted_out_user_is_never_mailed(conn, monkeypatch):
    row = seed_user(conn, digest_opt_out=1)
    add_match(conn, row["id"])
    sent = _capture(monkeypatch)
    digest.run(conn)
    assert sent == []                          # not even considered
    assert [r["user_ref"] for r in digest.recipients(conn)] == []


def test_will_not_digest_twice_in_one_week(conn, monkeypatch):
    row = seed_user(conn)
    add_match(conn, row["id"], tag="1")
    sent = _capture(monkeypatch)
    digest.send_one(conn, dict(row))
    add_match(conn, row["id"], created_at="2099-01-01T00:00:00", tag="2")
    out = digest.send_one(conn, dict(row))     # new match, but same week
    assert "already digested this week" in out and len(sent) == 1


def test_last_week_send_never_blocks_this_weeks(conn, monkeypatch):
    """The calendar-week rule (Joe, 17 Aug): a mid-week manual send must not
    block the following Monday. Anchor is Monday 00:00, not now-minus-6-days.
    """
    row = seed_user(conn)
    add_match(conn, row["id"], tag="1")
    sent = _capture(monkeypatch)
    digest.send_one(conn, dict(row))
    # shove the send event to just BEFORE this week's Monday — i.e. any day
    # last week, even Sunday 23:59 (which a 6-day rolling guard would block)
    ws = digest.week_start(conn)
    conn.execute("UPDATE events SET created_at = datetime(?, '-1 minute')"
                 " WHERE event_type='signup_email'", (ws,))
    conn.commit()
    assert not digest.sent_this_week(conn, "tuser")
    out = digest.send_one(conn, dict(row))
    assert "sent [" in out and len(sent) == 2


def test_dry_run_composes_but_sends_nothing(conn, monkeypatch):
    row = seed_user(conn)
    add_match(conn, row["id"])
    sent = _capture(monkeypatch)
    out = digest.send_one(conn, dict(row), dry_run=True)
    assert "WOULD send" in out
    assert sent == []
    assert conn.execute("SELECT COUNT(*) c FROM events"
                        " WHERE event_type='signup_email'").fetchone()["c"] == 0


# ---- content ----------------------------------------------------------------------

def test_digest_carries_opt_out_line_and_profile_edit_link(conn, monkeypatch):
    row = seed_user(conn)
    add_match(conn, row["id"], score=9, tag="hit")
    add_match(conn, row["id"], score=5, tag="near")     # near miss
    sent = _capture(monkeypatch)
    digest.send_one(conn, dict(row))
    html, text = sent[0]["html_body"], sent[0]["text_body"]
    assert "Reply STOP" in html and "Reply STOP" in text
    assert "/profile/tuser/" in html                     # profile-edit nudge
    assert "Conear" in html                              # near miss mentioned
    assert "Cohit" in html


def test_below_threshold_matches_send_the_near_only_variant(conn, monkeypatch):
    row = seed_user(conn)
    add_match(conn, row["id"], score=5, tag="low")
    sent = _capture(monkeypatch)
    out = digest.send_one(conn, dict(row))
    assert "sent [near_only]" in out and len(sent) == 1
    html = sent[0]["html_body"]
    assert "Colow" in html and "5/10" in html
    assert "close" in sent[0]["subject"].lower()
    assert "job titles" in html and "/profile/tuser/" in html
    for banned in ("lack", "missing", "gap"):
        assert banned not in html.lower()


# ---- confirmation reminders -------------------------------------------------------

def _bounce(conn, email):
    conn.execute(
        "INSERT INTO events (event_type, payload_json, created_at) VALUES"
        " ('email:BOUNCED', ?, datetime('now'))",
        ('{"to": "%s"}' % email,))
    conn.commit()


def test_unconfirmed_user_gets_a_confirm_reminder(conn, monkeypatch):
    seed_user(conn, "ghost", email="ghost@x.com", email_confirmed_at=None,
              active=0)
    sent = _capture(monkeypatch)
    stats = digest.run(conn)
    assert stats["reminded"] == 1 and len(sent) == 1
    assert sent[0]["to"] == "ghost@x.com"
    assert "/confirm/ghost/" in sent[0]["html_body"]
    ev = conn.execute(
        "SELECT COUNT(*) c FROM events WHERE event_type='signup_email'"
        " AND json_extract(payload_json,'$.kind')='confirm_reminder'"
        " AND json_extract(payload_json,'$.ok')=1").fetchone()["c"]
    assert ev == 1


def test_reminders_cap_at_two_ever(conn, monkeypatch):
    seed_user(conn, "ghost", email="ghost@x.com", email_confirmed_at=None,
              active=0)
    sent = _capture(monkeypatch)
    for _ in range(4):
        # each pass: shove events into the past so the weekly guard clears
        # but the lifetime count survives
        digest.run(conn)
        conn.execute("UPDATE events SET created_at = '2020-01-01T00:00:00'"
                     " WHERE event_type='signup_email'")
        conn.commit()
    assert len(sent) == digest.REMINDER_MAX


def test_bounced_address_is_never_reminded(conn, monkeypatch):
    seed_user(conn, "ghost", email="dead@x.com", email_confirmed_at=None,
              active=0)
    _bounce(conn, "dead@x.com")
    sent = _capture(monkeypatch)
    stats = digest.run(conn)
    assert stats["reminded"] == 0 and sent == []


def test_opted_out_unconfirmed_is_never_reminded(conn, monkeypatch):
    seed_user(conn, "ghost", email="g@x.com", email_confirmed_at=None,
              active=0, digest_opt_out=1)
    sent = _capture(monkeypatch)
    digest.run(conn)
    assert sent == []
