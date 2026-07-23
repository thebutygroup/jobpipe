import os

import django
from django.test import Client

from jobpipe.db import upsert_posting
from jobpipe.models import PostingDTO


def setup_module(module):
    os.environ["JOBPIPE_TESTING"] = "1"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "jobpipe.dashboard.settings")
    django.setup()


def seed_pending(conn, answers_json='{}'):
    conn.execute("INSERT INTO applicants (name, profile_path) VALUES ('t','p')")
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name="Acme", source="ats", external_id="1",
        title="Senior Data Engineer", location="London",
        apply_url="https://boards.greenhouse.io/acme/1", description_text="d"))
    conn.execute("INSERT INTO matches (posting_id, applicant_id, score, reasons_json,"
                 " red_flags_json, extracted_questions_json, model, tokens_used, created_at)"
                 " VALUES (?,1,9,'[\"great fit\"]','[\"onsite 5 days\"]','[]','m',10,"
                 " datetime('now'))", (pid,))
    conn.execute("INSERT INTO applications (posting_id, applicant_id, state, answers_json,"
                 " created_at, updated_at) VALUES (?,1,'PENDING_REVIEW',?,datetime('now'),"
                 "datetime('now'))", (pid, answers_json))
    conn.commit()
    return conn.execute("SELECT id FROM applications").fetchone()["id"]


def _point_db(monkeypatch, conn):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "db_path",
                        conn.execute("PRAGMA database_list").fetchone()["file"])


def test_queue_and_detail_render(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    app_id = seed_pending(conn)
    client = Client()
    r1 = client.get("/queue")
    assert r1.status_code == 200 and b"Acme" in r1.content and b"9/10" in r1.content
    r2 = client.get(f"/app/{app_id}")
    assert r2.status_code == 200 and b"Senior Data Engineer" in r2.content
    assert client.get("/healthz").content == b"ok"


def test_read_routes_reject_post(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    client = Client()
    for path in ("/", "/all", "/healthz"):
        assert client.post(path).status_code == 405, f"POST allowed on {path}"


def test_approve_blocked_by_unknown_field(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    answers = '{"sponsor": {"label": "visa", "required": true, "value": "", "unknown": true}}'
    app_id = seed_pending(conn, answers_json=answers)
    client = Client()
    # CSRF disabled path: use enforce_csrf_checks=False client (default)
    resp = client.post(f"/app/{app_id}/approve")
    assert resp.status_code == 400  # cannot approve with unresolved required field
    state = conn.execute("SELECT state FROM applications WHERE id=?",
                         (app_id,)).fetchone()["state"]
    assert state == "PENDING_REVIEW"  # unchanged


def test_approve_succeeds_when_complete(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    answers = '{"name": {"label": "Name", "required": true, "value": "Joe", "unknown": false}}'
    app_id = seed_pending(conn, answers_json=answers)
    client = Client()
    resp = client.post(f"/app/{app_id}/approve")
    assert resp.status_code == 302  # redirect to queue
    state = conn.execute("SELECT state FROM applications WHERE id=?",
                         (app_id,)).fetchone()["state"]
    assert state == "APPROVED"


def test_sources_page_renders(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    # one deduped job seen by two sources
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name="Sunny Days Nursery", source="ats", source_detail="greenhouse",
        external_id="1", title="Trainee Preschooler", location="London",
        apply_url="https://boards.greenhouse.io/sunny/1"))
    upsert_posting(conn, PostingDTO(
        company_name="Sunny Days Nursery Ltd", source="adzuna", external_id="9",
        title="Trainee Pre-Schooler", location="London",
        apply_url="https://adzuna.co.uk/land/ad/9", source_detail="adzuna"))
    conn.commit()
    r = Client().get("/sources")
    assert r.status_code == 200
    for needle in (b"Overlap matrix", b"greenhouse", b"adzuna", b"unconfigured"):
        assert needle in r.content, needle


def seed_two_users(conn):
    """Two applicants with their own matched postings."""
    conn.execute("INSERT INTO applicants (name, profile_path, user_ref) "
                 "VALUES ('joebuty','p','joebuty')")
    conn.execute("INSERT INTO applicants (name, profile_path, user_ref) "
                 "VALUES ('otheruser','p','otheruser')")
    for i, (aid, title) in enumerate([(1, "Senior Data Engineer"),
                                      (2, "Marketing Lead")], start=1):
        pid, _ = upsert_posting(conn, PostingDTO(
            company_name=f"Co{i}", source="ats", external_id=str(i), title=title,
            location="London", apply_url=f"https://boards.greenhouse.io/co{i}/{i}",
            description_text="d"))
        conn.execute("INSERT INTO matches (posting_id, applicant_id, score, reasons_json,"
                     " red_flags_json, extracted_questions_json, model, tokens_used,"
                     " created_at) VALUES (?,?,8,'[]','[]','[]','m',1,datetime('now'))",
                     (pid, aid))
        conn.execute("INSERT INTO applications (posting_id, applicant_id, state,"
                     " answers_json, created_at, updated_at) VALUES (?,?,'MATCHED','{}',"
                     " datetime('now'),datetime('now'))", (pid, aid))
    conn.commit()


def test_public_all_view_scoped_to_user(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    seed_two_users(conn)
    r = Client().get("/all/joebuty")
    assert r.status_code == 200
    html = r.content.decode()
    assert "Senior Data Engineer" in html and "Co1" in html
    # no leakage: other user's rows, names, and internal links are absent
    assert "Marketing Lead" not in html and "Co2" not in html
    assert "otheruser" not in html
    assert "/app/" not in html                      # internal review links
    assert 'href="/queue"' not in html              # internal nav hidden
    assert "/job_matches/joebuty/" in html          # rows link to public detail


def test_public_all_view_unknown_ref_404(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    assert Client().get("/all/nobody-here").status_code == 404


def test_public_all_view_rejects_post(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    seed_two_users(conn)
    assert Client().post("/all/joebuty").status_code == 405


# ---- self-serve signup: minimal fields, auto-activation, daily cap ------------------

def _signup(client, name, **extra):
    return client.post("/onboard", {
        "full_name": name, "target_titles": "Senior Data Engineer",
        "positioning": "Hands-on data platform work at a company that ships.",
        **extra})


def test_minimal_signup_activates_and_starts_matching(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    from jobpipe.dashboard import views as v
    runs = []
    monkeypatch.setattr(v, "_instant_mini_run", lambda ref: runs.append(ref))
    r = _signup(Client(), "maya")
    assert r.status_code == 200 and b"maya" in r.content
    assert b"Matching has started" in r.content
    row = conn.execute("SELECT active, user_ref, profile_yaml FROM applicants").fetchone()
    assert row["active"] == 1 and row["user_ref"] == "maya"
    assert "Senior Data Engineer" in row["profile_yaml"]
    assert runs == ["maya"]  # instant mini-run kicked off


def test_signup_requires_title_and_sentence(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    c = Client()
    r = c.post("/onboard", {"full_name": "maya", "target_titles": "",
                            "positioning": "something"})
    assert r.status_code == 400
    r = c.post("/onboard", {"full_name": "maya", "target_titles": "Data Engineer",
                            "positioning": "  "})
    assert r.status_code == 400
    assert conn.execute("SELECT COUNT(*) c FROM applicants").fetchone()["c"] == 0


def test_signup_email_is_optional(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    from jobpipe.dashboard import views as v
    monkeypatch.setattr(v, "_instant_mini_run", lambda ref: None)
    assert _signup(Client(), "noemail").status_code == 200
    assert _signup(Client(), "hasemail", email="x@y.com").status_code == 200


def test_signup_daily_cap_flags_for_joe(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    from jobpipe.config import settings
    from jobpipe.dashboard import views as v
    monkeypatch.setattr(settings, "signup_daily_cap", 2)
    monkeypatch.setattr(v, "_instant_mini_run", lambda ref: None)
    emails = []
    import jobpipe.notify as notify
    monkeypatch.setattr(notify, "send_email", lambda **kw: emails.append(kw) or True)
    c = Client()
    assert b"Matching has started" in _signup(c, "one").content
    assert b"Matching has started" in _signup(c, "two").content
    r3 = _signup(c, "three")  # over cap -> pending + flagged
    assert r3.status_code == 200 and b"human review" in r3.content
    row = conn.execute("SELECT active FROM applicants WHERE user_ref='three'").fetchone()
    assert row["active"] == 0
    assert v.signups_capped_today(conn) == 1
    assert emails and "cap hit" in emails[-1]["subject"]  # last email = the capped one
    # the flag is visible on internal pages
    q = c.get("/queue")
    assert b"auto-activation cap" in q.content
    a = c.get("/applicants")
    assert b"auto-activation cap" in a.content and b"awaiting activation" in a.content


def test_every_signup_notifies_joe(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    from jobpipe.config import settings
    from jobpipe.dashboard import views as v
    monkeypatch.setattr(settings, "signup_daily_cap", 1)
    monkeypatch.setattr(v, "_instant_mini_run", lambda ref: None)
    emails = []
    import jobpipe.notify as notify
    monkeypatch.setattr(notify, "send_email", lambda **kw: emails.append(kw) or True)
    c = Client()
    _signup(c, "first")   # auto-activated
    _signup(c, "second")  # cap hit
    assert len(emails) == 2
    assert "new signup: first" in emails[0]["subject"] and "1/1" in emails[0]["subject"]
    assert "PENDING: second" in emails[1]["subject"]
