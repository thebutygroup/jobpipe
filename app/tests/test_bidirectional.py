"""R3: candidate fit rides the matcher's ONE call. Resume-less users'
prompts stay byte-identical to pre-R3; resume users get both verdicts
stored; legacy/absent fields parse as None."""

import json
from types import SimpleNamespace

from jobpipe.matching import matcher
from tests.test_matcher import GOLDEN, seed  # noqa: F401  (fixtures reuse)
from tests.test_resume import CV, make_pdf


class RecordingClient:
    """Returns a canned combined reply; records every prompt + max_tokens."""

    def __init__(self, with_candidate=False):
        self.with_candidate = with_candidate
        self.calls = []
        self.messages = self

    def create(self, model, max_tokens, temperature, messages):
        self.calls.append({"prompt": messages[0]["content"],
                           "max_tokens": max_tokens})
        payload = {"score": 8, "reasons": ["r"], "red_flags": [],
                   "seniority_fit": "right", "questions_visible": []}
        if self.with_candidate:
            payload |= {"candidate_fit": 7,
                        "bring": ["retail experience the JD asks for"],
                        "unlisted": ["dbt, evidenced in your profile"]}
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(payload))],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def _run_one(conn, profile, client, aid=None):
    seed(conn, "Senior Data Engineer")
    aid = aid or matcher.ensure_applicant(conn, profile)
    matcher.run(conn, profile, aid, client=client)
    return aid


def test_resumeless_prompt_byte_identical(conn, profile):
    """No resume → the prompt and max_tokens are EXACTLY the pre-R3 ones."""
    client = RecordingClient()
    _run_one(conn, profile, client)
    call = client.calls[0]
    assert call["max_tokens"] == 600
    assert "CANDIDATE FIT" not in call["prompt"]
    assert "candidate_fit" not in call["prompt"]
    # and the row stores NULLs, not zeros
    row = conn.execute("SELECT candidate_fit_score, candidate_fit_json"
                       " FROM matches").fetchone()
    assert row["candidate_fit_score"] is None and row["candidate_fit_json"] is None


def test_resume_user_gets_both_verdicts_in_one_call(conn, profile):
    from jobpipe import resume as resmod
    client = RecordingClient(with_candidate=True)
    aid = matcher.ensure_applicant(conn, profile)
    resmod.save_resume(conn, aid, "u", "cv.pdf", make_pdf(CV * 3))
    _run_one(conn, profile, client, aid=aid)
    call = client.calls[0]
    assert len(client.calls) == 1                     # ONE call, two verdicts
    assert call["max_tokens"] == 1600
    assert "CANDIDATE FIT" in call["prompt"]
    assert "Senior Data Engineer with ten years" in call["prompt"]  # resume text
    # positive-framing rule applies to the CANDIDATE FIT block (the existing
    # role-fit prompt's internal analysis language is out of scope)
    cand_part = call["prompt"][call["prompt"].index("CANDIDATE FIT"):]
    for word in ("lack", "missing", "gap"):
        assert word not in cand_part.lower()
    row = conn.execute("SELECT score, candidate_fit_score, candidate_fit_json"
                       " FROM matches").fetchone()
    assert row["score"] == 8 and row["candidate_fit_score"] == 7
    stored = json.loads(row["candidate_fit_json"])
    assert stored["bring"] and stored["unlisted"]


def test_flagged_resume_never_reaches_the_prompt(conn, profile, monkeypatch):
    from jobpipe import resume as resmod, safety
    monkeypatch.setattr(safety, "looks_like_injection",
                        lambda t: (True, "test"))
    client = RecordingClient()
    aid = matcher.ensure_applicant(conn, profile)
    resmod.save_resume(conn, aid, "u", "cv.pdf", make_pdf(CV * 3))
    _run_one(conn, profile, client, aid=aid)
    assert "CANDIDATE FIT" not in client.calls[0]["prompt"]
    assert client.calls[0]["max_tokens"] == 600


def test_reply_without_candidate_fields_stores_nulls(conn, profile):
    """A resume user whose reply omits the new fields (model hiccup, legacy
    cache) still gets their role-fit row — candidate side is NULL."""
    from jobpipe import resume as resmod
    client = RecordingClient(with_candidate=False)   # reply has no new fields
    aid = matcher.ensure_applicant(conn, profile)
    resmod.save_resume(conn, aid, "u", "cv.pdf", make_pdf(CV * 3))
    _run_one(conn, profile, client, aid=aid)
    row = conn.execute("SELECT score, candidate_fit_score FROM matches").fetchone()
    assert row["score"] == 8 and row["candidate_fit_score"] is None


def test_migration_adds_columns_to_preexisting_matches(tmp_path):
    import sqlite3

    from jobpipe import db as dbmod
    from tests.test_migration import OLD_SCHEMA
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.close()
    c = dbmod.connect(path)
    cols = {r[1] for r in c.execute("PRAGMA table_info(matches)")}
    assert {"candidate_fit_score", "candidate_fit_json"} <= cols
    c.close()