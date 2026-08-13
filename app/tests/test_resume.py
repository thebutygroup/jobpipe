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