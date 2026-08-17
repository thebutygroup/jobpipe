from jobpipe.prepare.answers import resolve
from jobpipe.prepare.forms import FormField, extract_from_html
from jobpipe.profile import (Documents, Eligibility, Identity, Preferences,
                             Profile, Salary)

GH_FORM = """
<form id="application_form">
  <label for="first_name">First Name *</label>
  <input id="first_name" name="first_name" required>
  <label for="email">Email *</label><input id="email" name="email" type="email" required>
  <label for="phone">Phone</label><input id="phone" name="phone" type="tel">
  <label for="sponsor">Do you require visa sponsorship? *</label>
  <select id="sponsor" name="sponsor" required>
    <option></option><option>Yes</option><option>No</option></select>
  <label for="salary">Salary expectation</label><input id="salary" name="salary">
  <label for="why">Why do you want to work here? *</label>
  <textarea id="why" name="why" required></textarea>
  <label for="resume">Resume *</label><input id="resume" name="resume" type="file" required>
  <input type="hidden" name="csrf" value="x">
  <button type="submit">Apply</button>
</form>"""


def _profile(sponsorship=False, salary_pref=95000):
    return Profile(
        identity=Identity(full_name="Joe Buty", email="joe@example.com",
                          location="London, UK",
                          links={"phone": "+44 700 000000", "linkedin": "x"}),
        preferences=Preferences(target_titles=["Senior Data Engineer"],
                                locations_ok=["London"]),
        eligibility=Eligibility(requires_sponsorship=sponsorship,
                                salary=Salary(min=85000, preferred=salary_pref)),
        documents=Documents(resume_default="assets/resume.pdf"),
    )


def test_extract_fields_skips_hidden_and_submit():
    fields = extract_from_html(GH_FORM)
    keys = {f.key for f in fields}
    assert keys == {"first_name", "email", "phone", "sponsor", "salary", "why", "resume"}
    assert next(f for f in fields if f.key == "why").kind == "textarea"
    assert next(f for f in fields if f.key == "sponsor").options == ["Yes", "No"]
    assert next(f for f in fields if f.key == "resume").kind == "file"


def test_identity_resolution():
    p = _profile()
    assert resolve(FormField("first_name", "First Name", "text"), p).value == "Joe"
    assert resolve(FormField("email", "Email", "email"), p).value == "joe@example.com"


def test_sponsorship_resolves_structured_only():
    p = _profile(sponsorship=False)
    f = FormField("sponsor", "Do you require visa sponsorship?", "select",
                  options=["Yes", "No"])
    r = resolve(f, p)
    assert r.value == "No" and r.source == "structured" and not r.llm


def test_sponsorship_unknown_when_unset():
    p = _profile()
    p.eligibility.requires_sponsorship = None
    f = FormField("sponsor", "Do you require visa sponsorship?", "select",
                  options=["Yes", "No"])
    assert resolve(f, p).unknown


def test_salary_never_llm():
    p = _profile(salary_pref=95000)
    r = resolve(FormField("salary", "Salary expectation", "text"), p)
    assert r.source == "structured" and not r.llm and "95" in r.value


def test_free_text_routes_to_llm():
    p = _profile()
    r = resolve(FormField("why", "Why do you want to work here?", "textarea"), p)
    assert r.llm and r.source == "llm"


def test_resume_file_resolves():
    p = _profile()
    r = resolve(FormField("resume", "Resume", "file"), p)
    assert r.source == "file" and r.value == "assets/resume.pdf"


def test_compliance_select_no_fuzzy_guess():
    # A sponsorship value that doesn't cleanly map must be UNKNOWN, not guessed.
    p = _profile(sponsorship=True)
    f = FormField("sponsor", "visa sponsorship", "select",
                  options=["I have the right to work", "I need a different visa"])
    assert resolve(f, p).unknown  # "Yes" matches neither cleanly


# ---- dead apply-URL retirement (run loop) -----------------------------------
# Regression for the accumulating-failures problem: apps whose apply URL 404'd
# stayed MATCHED forever, re-failing every run (81 -> 90 -> ... on /health).

from jobpipe.db import upsert_posting  # noqa: E402
from jobpipe.models import PostingDTO  # noqa: E402
from jobpipe.pollers.base import FetchError  # noqa: E402
from jobpipe.prepare import preparer  # noqa: E402


def _matched_app(conn, url="https://boards.example/jobs/1"):
    conn.execute("INSERT INTO applicants (name, profile_path) VALUES ('t','p')")
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name="Acme", source="ats", external_id="dead1", title="SDE",
        location="London", apply_url=url))
    conn.execute("INSERT INTO applications (posting_id, applicant_id, state,"
                 " created_at, updated_at)"
                 " VALUES (?,1,'MATCHED',datetime('now'),datetime('now'))", (pid,))
    conn.commit()
    return conn.execute("SELECT id FROM applications").fetchone()["id"], pid


def _run_with_fetch(conn, monkeypatch, exc):
    def boom(url):
        raise exc
    monkeypatch.setattr(preparer, "extract_from_url", boom)
    monkeypatch.setattr(preparer, "load_applicant_profile", lambda row: _profile())
    return preparer.run(conn, client=None)


def test_dead_url_retires_app_and_closes_posting(conn, monkeypatch):
    app_id, pid = _matched_app(conn)
    stats = _run_with_fetch(conn, monkeypatch,
                            FetchError("HTTP 404 for x", status=404))
    assert stats == {"prepared": 0, "needs_browser": 0, "failed": 0, "dead_url": 1}
    row = conn.execute("SELECT state, review_notes FROM applications WHERE id=?",
                       (app_id,)).fetchone()
    assert row["state"] == "FAILED"
    assert "HTTP 404" in row["review_notes"]
    assert conn.execute("SELECT closed_at FROM postings WHERE id=?",
                        (pid,)).fetchone()["closed_at"] is not None
    ev = conn.execute("SELECT COUNT(*) FROM events WHERE event_type ="
                      " 'prepare:dead_url'").fetchone()[0]
    assert ev == 1


def test_dead_url_not_retried_next_run(conn, monkeypatch):
    _matched_app(conn)
    _run_with_fetch(conn, monkeypatch, FetchError("HTTP 404 for x", status=404))
    stats2 = _run_with_fetch(conn, monkeypatch,
                             FetchError("HTTP 404 for x", status=404))
    # app is FAILED now, so matched_apps() no longer returns it
    assert stats2["dead_url"] == 0 and stats2["failed"] == 0


def test_transient_fetch_error_stays_matched(conn, monkeypatch):
    app_id, pid = _matched_app(conn)
    stats = _run_with_fetch(conn, monkeypatch,
                            FetchError("failed after 3 attempts: x"))  # no status
    assert stats["failed"] == 1 and stats["dead_url"] == 0
    assert conn.execute("SELECT state FROM applications WHERE id=?",
                        (app_id,)).fetchone()["state"] == "MATCHED"
    assert conn.execute("SELECT closed_at FROM postings WHERE id=?",
                        (pid,)).fetchone()["closed_at"] is None


def test_forbidden_routes_to_needs_browser(conn, monkeypatch):
    # 403 bot walls (Adzuna landing pages) are a browser problem, not a
    # failure — flag for the submitter's Playwright pass, never close.
    app_id, pid = _matched_app(conn)
    stats = _run_with_fetch(conn, monkeypatch,
                            FetchError("HTTP 403 for x", status=403))
    assert stats["needs_browser"] == 1
    assert stats["failed"] == 0 and stats["dead_url"] == 0
    row = conn.execute("SELECT state, review_notes FROM applications WHERE id=?",
                       (app_id,)).fetchone()
    # the exact string submit/runner.py selects on
    assert row["state"] == "MATCHED"
    assert row["review_notes"] == preparer.NEEDS_BROWSER_NOTE
    assert conn.execute("SELECT closed_at FROM postings WHERE id=?",
                        (pid,)).fetchone()["closed_at"] is None


def test_needs_browser_apps_not_refetched(conn, monkeypatch):
    # Once flagged, the submitter owns it: prepare must stop burning a
    # rate-limited fetch on it every single run (162/day at its worst).
    _matched_app(conn)
    _run_with_fetch(conn, monkeypatch, FetchError("HTTP 403 for x", status=403))
    calls = []

    def counting(url):
        calls.append(url)
        raise FetchError("HTTP 403 for x", status=403)
    monkeypatch.setattr(preparer, "extract_from_url", counting)
    monkeypatch.setattr(preparer, "load_applicant_profile", lambda row: _profile())
    stats = preparer.run(conn, client=None)
    assert calls == [] and stats["needs_browser"] == 0
