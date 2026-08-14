"""Resume intake (R1 of the bidirectional-match design): PDF in, plain text
kept, PDF discarded. Token-gated upload; flagged text never reaches a
prompt."""

import io
import os

import django
import pytest
from django.test import Client

from jobpipe import resume as resmod


def setup_module(module):
    os.environ["JOBPIPE_TESTING"] = "1"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "jobpipe.dashboard.settings")
    django.setup()


def make_pdf(text: str) -> bytes:
    """A minimal, valid one-page PDF whose text pypdf can extract."""
    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]"
        b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n",
        b"4 0 obj<</Length " + str(len(stream)).encode() + b">>stream\n"
        + stream + b"\nendstream endobj\n",
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for o in objs:
        offsets.append(out.tell())
        out.write(o)
    xref = out.tell()
    out.write(b"xref\n0 6\n0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n"
              + str(xref).encode() + b"\n%%EOF")
    return out.getvalue()


CV = ("Senior Data Engineer with ten years building Spark and dbt pipelines "
      "across retail and advertising. Led a team of five. AWS, Python, SQL. ")


def _seed(conn, token="tok123"):
    conn.execute("INSERT INTO applicants (name, user_ref, profile_path,"
                 " edit_token) VALUES ('T','tuser','p',?)", (token,))
    conn.commit()
    return conn.execute("SELECT id FROM applicants").fetchone()["id"]


# ---- extract_text ------------------------------------------------------------

def test_rejections_by_code():
    for data, code in ((b"", "nofile"),
                       (b"MZ not a pdf at all", "notpdf"),
                       (b"%PDF-" + b"x" * (resmod.MAX_SIZE_BYTES + 1), "toobig"),
                       (b"%PDF-1.4 garbage that pypdf cannot parse", "noread")):
        with pytest.raises(resmod.ResumeError) as e:
            resmod.extract_text(data)
        assert e.value.code == code, code


def test_scanned_pdf_with_no_text_is_rejected():
    with pytest.raises(resmod.ResumeError) as e:
        resmod.extract_text(make_pdf("short"))
    assert e.value.code == "notext"


def test_real_pdf_extracts_normalised_text():
    text = resmod.extract_text(make_pdf(CV * 3))
    assert "Senior Data Engineer" in text and "dbt" in text
    assert "  " not in text            # whitespace normalised


# ---- save/replace + the flagged gate ----------------------------------------

def test_save_replace_and_no_pdf_bytes_stored(conn):
    aid = _seed(conn)
    pdf = make_pdf(CV * 3)
    row = resmod.save_resume(conn, aid, "tuser", "my cv.pdf", pdf)
    assert row["filename"] == "my cv.pdf" and row["upload_size_bytes"] == len(pdf)
    assert "Senior Data Engineer" in row["resume_text"]
    assert "content" not in row        # text only — the PDF is discarded
    ev = conn.execute("SELECT payload_json FROM events WHERE"
                      " event_type='resume:UPLOADED'").fetchone()
    assert ev and "sha256" in ev["payload_json"]
    # replace updates the same row
    row2 = resmod.save_resume(conn, aid, "tuser", "cv2.pdf",
                              make_pdf((CV + "Now with Kafka. ") * 3))
    assert row2["id"] == row["id"] and row2["filename"] == "cv2.pdf"
    assert conn.execute("SELECT COUNT(*) AS n FROM resumes").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM events WHERE"
                        " event_type='resume:REPLACED'").fetchone()["n"] == 1


def test_flagged_text_never_reaches_a_prompt(conn, monkeypatch):
    aid = _seed(conn)
    from jobpipe import safety
    monkeypatch.setattr(safety, "looks_like_injection",
                        lambda t: (True, "test-flag"))
    resmod.save_resume(conn, aid, "tuser", "cv.pdf", make_pdf(CV * 3))
    row = resmod.get_resume(conn, aid)
    assert row["text_flagged"] == 1
    assert resmod.prompt_text_for(conn, aid) == ""   # the single safe accessor


def test_migration_applies_to_preexisting_db(tmp_path):
    import sqlite3

    from jobpipe import db as dbmod
    from tests.test_migration import OLD_SCHEMA
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.close()
    for _ in (1, 2):
        c = dbmod.connect(path)
        cols = {r[1] for r in c.execute("PRAGMA table_info(resumes)")}
        assert {"applicant_id", "label", "resume_text", "text_flagged",
                "upload_sha256"} <= cols
        c.close()


# ---- the upload route (token-gated) -----------------------------------------

def _point_db(monkeypatch, conn):
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "db_path",
                        conn.execute("PRAGMA database_list").fetchone()["file"])


def test_upload_route_end_to_end(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    _seed(conn)
    c = Client()
    # wrong token: 404, nothing stored
    r = c.post("/profile/tuser/WRONG/resume",
               {"resume": io.BytesIO(make_pdf(CV * 3))})
    assert r.status_code == 404
    # non-PDF: redirected back with the coded error, nothing stored
    bad = io.BytesIO(b"plain text pretending")
    bad.name = "cv.pdf"
    r = c.post("/profile/tuser/tok123/resume", {"resume": bad})
    assert r.status_code == 302 and "resume_error=notpdf" in r.headers["Location"]
    assert conn.execute("SELECT COUNT(*) AS n FROM resumes").fetchone()["n"] == 0
    # happy path: stored, redirected as saved, card renders the filename
    good = io.BytesIO(make_pdf(CV * 3))
    good.name = "my-cv.pdf"
    r = c.post("/profile/tuser/tok123/resume", {"resume": good})
    assert r.status_code == 302 and "resume_saved=1" in r.headers["Location"]
    page = c.get("/profile/tuser/tok123").content
    assert b"my-cv.pdf" in page and b"Replace resume" in page
    assert b"Resume saved" in page or b"resume_saved" not in page  # card shown


def test_error_codes_never_reflect_url_text(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    _seed(conn)
    page = Client().get("/profile/tuser/tok123",
                        {"resume_error": "your account is suspended call 555"})
    assert b"account is suspended" not in page.content

# ---- discovery: the anonymous matches page's door to the profile page --------

def test_matches_page_banner_and_profile_link_email(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    aid = _seed(conn)
    conn.execute("UPDATE applicants SET email_confirmed_at='2026-08-01T00:00:00',"
                 " profile_yaml=? WHERE id=?",
                 ("identity:\n  full_name: T\n  email: t@example.com\n"
                  "preferences:\n  target_titles: [Data Engineer]\n"
                  "  locations_ok: [London]\n", aid))
    conn.commit()
    sent = []
    from jobpipe import notify
    monkeypatch.setattr(notify, "send_email",
                        lambda **kw: sent.append(kw) or True)
    c = Client()
    # banner shows for resume-less users, with the email-me button
    page = c.get("/job_matches/tuser").content
    assert b"Add your resume" in page and b"Email me my profile link" in page
    # button emails the private link to the address on file
    r = c.post("/job_matches/tuser/profile_link")
    assert r.status_code == 302 and "plink=sent" in r.headers["Location"]
    assert len(sent) == 1 and sent[0]["to"] == "t@example.com"
    assert "/profile/tuser/" in sent[0]["html_body"]
    # rate-limited: second click inside a day sends nothing, same response
    r2 = c.post("/job_matches/tuser/profile_link")
    assert "plink=sent" in r2.headers["Location"] and len(sent) == 1
    # unknown user: identical response shape, nothing sent (no oracle)
    r3 = c.post("/job_matches/nobody/profile_link")
    assert "plink=sent" in r3.headers["Location"] and len(sent) == 1
    # once a resume exists the upsell flips to a persistent profile door
    resmod.save_resume(conn, aid, "tuser", "cv.pdf", make_pdf(CV * 3))
    page = c.get("/job_matches/tuser").content
    assert b"Add your resume" not in page
    assert b"Resume on file" in page
    assert b"Email me my profile link" in page   # the door never disappears


def test_unconfirmed_user_gets_no_profile_link_email(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    _seed(conn)   # email_confirmed_at stays NULL
    sent = []
    from jobpipe import notify
    monkeypatch.setattr(notify, "send_email",
                        lambda **kw: sent.append(kw) or True)
    r = Client().post("/job_matches/tuser/profile_link")
    assert "plink=sent" in r.headers["Location"] and sent == []


# ---- /profile/ gate: private for users, index for the super user -------------

def test_profile_gate_public_and_shortcut(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    _seed(conn)
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "admin_key", "")
    c = Client()
    # /profile/ — private gate with a username form; no tokens anywhere
    page = c.get("/profile/").content
    assert b"Profiles are private" in page and b'name="u"' in page
    assert b"tok123" not in page
    # the form routes to the per-user door
    r = c.get("/profile/", {"u": "tuser"})
    assert r.status_code == 302 and r.headers["Location"] == "/profile/tuser"
    # /profile/<ref> — email-me door, identical for unknown users (no oracle)
    known = c.get("/profile/tuser").content
    unknown = c.get("/profile/nobody").content
    assert b"Email me my profile link" in known
    assert b"Email me my profile link" in unknown
    assert b"tok123" not in known


def test_profile_gate_super_user_sees_everyone(conn, monkeypatch):
    _point_db(monkeypatch, conn)
    _seed(conn)
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "admin_key", "sekrit")
    c = Client()
    # keyed index lists every applicant with direct profile links
    page = c.get("/profile/", {"key": "sekrit"}).content
    assert b"All profiles" in page and b"/profile/tuser/tok123" in page
    assert b"no resume" in page
    # keyed shortcut goes straight through to the token page
    r = c.get("/profile/tuser", {"key": "sekrit"})
    assert r.status_code == 302 and r.headers["Location"] == "/profile/tuser/tok123"
    # wrong key behaves like the public gate
    page = c.get("/profile/", {"key": "wrong"}).content
    assert b"Profiles are private" in page and b"tok123" not in page
