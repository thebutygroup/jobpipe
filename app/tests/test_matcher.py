import json
from types import SimpleNamespace


from jobpipe.db import upsert_posting
from jobpipe.matching import matcher
from jobpipe.models import PostingDTO


class FakeClient:
    """Returns canned scores keyed by posting title; counts calls."""

    def __init__(self, scores: dict[str, int], malformed_first_for: set[str] = frozenset()):
        self.scores = scores
        self.malformed_first_for = set(malformed_first_for)
        self.calls = 0
        self.messages = self

    def create(self, model, max_tokens, temperature, messages):
        self.calls += 1
        prompt = messages[0]["content"]
        title = next(t for t in self.scores if f"Title: {t}" in prompt)
        if title in self.malformed_first_for:
            self.malformed_first_for.discard(title)
            text = "not json at all {"
        else:
            text = json.dumps({"score": self.scores[title], "reasons": ["r"],
                               "red_flags": [], "seniority_fit": "right",
                               "questions_visible": []})
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def seed(conn, title):
    pid, _ = upsert_posting(conn, PostingDTO(
        company_name="Acme", source="ats", external_id=title, title=title,
        location="London", apply_url=f"https://boards.greenhouse.io/acme/{title}",
        description_text=f"Role: {title}"))
    conn.execute("INSERT INTO events (posting_id, event_type, payload_json, created_at)"
                 " VALUES (?, 'prefilter:PREFILTERED', '{}', datetime('now'))", (pid,))
    conn.commit()
    return pid


# All titles are RELEVANT to the fixture profile (the matcher now skips
# irrelevant titles before spending a call) — the model's score alone decides
# the band.
GOLDEN = {
    "Forward Deployed Engineer": 9,     # obvious fit
    "Senior Data Engineer": 8,          # obvious fit
    "Machine Learning Engineer": 2,     # relevant title, weak posting
    "Applied AI Engineer": 1,           # relevant title, terrible posting
    "Data Engineering Manager": 6,      # borderline -> below threshold 7
}


def test_golden_scoring_bands(conn, profile):
    for t in GOLDEN:
        seed(conn, t)
    client = FakeClient(GOLDEN)
    aid = matcher.ensure_applicant(conn, profile)
    stats = matcher.run(conn, profile, aid, client=client)
    assert stats["matched"] == 2 and stats["rejected"] == 3
    matched_titles = {r["title"] for r in conn.execute(
        "SELECT p.title FROM applications a JOIN postings p ON p.id=a.posting_id")}
    assert matched_titles == {"Forward Deployed Engineer", "Senior Data Engineer"}


def test_no_rematch_of_unchanged_hash(conn, profile):
    seed(conn, "Senior Data Engineer")
    client = FakeClient({"Senior Data Engineer": 8})
    aid = matcher.ensure_applicant(conn, profile)
    matcher.run(conn, profile, aid, client=client)
    calls_after_first = client.calls
    matcher.run(conn, profile, aid, client=client)
    assert client.calls == calls_after_first  # nothing rematched


def test_malformed_output_retried_then_ok(conn, profile):
    seed(conn, "Senior Data Engineer")
    client = FakeClient({"Senior Data Engineer": 8},
                        malformed_first_for={"Senior Data Engineer"})
    aid = matcher.ensure_applicant(conn, profile)
    stats = matcher.run(conn, profile, aid, client=client)
    assert stats["matched"] == 1 and client.calls == 2


def test_daily_call_cap(conn, profile, monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "match_daily_call_cap", 2)
    for t in GOLDEN:
        seed(conn, t)
    client = FakeClient(GOLDEN)
    aid = matcher.ensure_applicant(conn, profile)
    stats = matcher.run(conn, profile, aid, client=client)
    assert stats["matched"] + stats["rejected"] == 2 and stats["capped"] == 1

def test_per_user_cap_leaves_budget_for_next_applicant(conn, profile, monkeypatch):
    """Fairness: applicant 1 stops at the per-user cap even though global
    budget remains, so applicant 2 still gets scored the same day."""
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "match_daily_call_cap", 100)
    monkeypatch.setattr(settings, "match_daily_call_cap_per_user", 2)
    for t in GOLDEN:
        seed(conn, t)
    client = FakeClient(GOLDEN)
    aid1 = matcher.ensure_applicant(conn, profile)
    conn.execute("INSERT INTO applicants (name, user_ref, profile_path)"
                 " VALUES ('Second User', 'second', 'p')")
    aid2 = conn.execute("SELECT id FROM applicants WHERE user_ref='second'"
                        ).fetchone()["id"]
    s1 = matcher.run(conn, profile, aid1, client=client)
    assert s1["matched"] + s1["rejected"] == 2 and s1["capped"] == 1
    s2 = matcher.run(conn, profile, aid2, client=FakeClient(GOLDEN))
    assert s2["matched"] + s2["rejected"] == 2  # second user got their turn


def test_per_user_cap_disabled_with_zero(conn, profile, monkeypatch):
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "match_daily_call_cap", 100)
    monkeypatch.setattr(settings, "match_daily_call_cap_per_user", 0)
    for t in GOLDEN:
        seed(conn, t)
    aid = matcher.ensure_applicant(conn, profile)
    stats = matcher.run(conn, profile, aid, client=FakeClient(GOLDEN))
    assert stats["matched"] + stats["rejected"] == len(GOLDEN)


class DeadClient:
    """Every call raises — the shape of a bad API key / dead network day."""

    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, model, max_tokens, temperature, messages):
        self.calls += 1
        raise RuntimeError("connection refused")


def test_failed_calls_count_toward_global_cap(conn, profile, monkeypatch):
    """4 Aug 2026: 1,091 failed calls sailed past a 400 cap because only
    successes were counted. Failures must spend budget too."""
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "match_daily_call_cap", 2)
    for t in GOLDEN:
        seed(conn, t)
    client = DeadClient()
    aid = matcher.ensure_applicant(conn, profile)
    stats = matcher.run(conn, profile, aid, client=client)
    assert stats["failed"] == 2 and stats["capped"] == 1
    assert client.calls == 2  # the cap actually stopped the burn


def test_failed_calls_persist_into_next_run(conn, profile, monkeypatch):
    """A rerun the same day starts already capped — no second burn."""
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "match_daily_call_cap", 2)
    for t in GOLDEN:
        seed(conn, t)
    aid = matcher.ensure_applicant(conn, profile)
    matcher.run(conn, profile, aid, client=DeadClient())
    assert matcher.calls_today(conn) == 2
    second = DeadClient()
    stats = matcher.run(conn, profile, aid, client=second)
    assert second.calls == 0 and stats["capped"] == 1


def test_failed_calls_count_toward_per_user_cap(conn, profile, monkeypatch):
    """Per-user fairness holds on a failing day: one applicant's failures
    can't eat the whole global budget, and the FAILED event records whose
    call it was."""
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "match_daily_call_cap", 100)
    monkeypatch.setattr(settings, "match_daily_call_cap_per_user", 2)
    for t in GOLDEN:
        seed(conn, t)
    aid = matcher.ensure_applicant(conn, profile)
    client = DeadClient()
    stats = matcher.run(conn, profile, aid, client=client)
    assert stats["failed"] == 2 and stats["capped"] == 1 and client.calls == 2
    assert matcher.calls_today_for(conn, aid) == 2
    tagged = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE event_type='match:FAILED'"
        " AND json_extract(payload_json,'$.applicant_id') = ?", (aid,)).fetchone()["n"]
    assert tagged == 2


# ---- llm_usage: real token accounting, one row per call ----------------------

def test_usage_row_per_evaluation(conn, profile):
    for t in GOLDEN:
        seed(conn, t)
    aid = matcher.ensure_applicant(conn, profile)
    matcher.run(conn, profile, aid, client=FakeClient(GOLDEN))
    rows = conn.execute("SELECT * FROM llm_usage ORDER BY id").fetchall()
    assert len(rows) == len(GOLDEN)
    for r in rows:
        assert r["ok"] == 1 and r["applicant_id"] == aid
        assert r["input_tokens"] == 100 and r["output_tokens"] == 50
        assert r["cache_read_tokens"] == 0 and r["batch_id"] is None
        assert r["posting_id"] is not None


def test_usage_recorded_for_failures_too(conn, profile, monkeypatch):
    """A dead call has no API-reported usage — zeros + ok=0 is the truth.
    Cap set to len(GOLDEN) so every posting gets exactly one attempt."""
    from jobpipe.config import settings

    monkeypatch.setattr(settings, "match_daily_call_cap", len(GOLDEN))
    for t in GOLDEN:
        seed(conn, t)
    aid = matcher.ensure_applicant(conn, profile)
    matcher.run(conn, profile, aid, client=DeadClient())
    rows = conn.execute("SELECT * FROM llm_usage").fetchall()
    assert len(rows) == len(GOLDEN)
    assert all(r["ok"] == 0 and r["input_tokens"] == 0 for r in rows)


def test_malformed_response_bills_both_attempts(conn, profile):
    """First attempt returns unparseable text: the API still billed it, so
    its REAL tokens land with ok=0; the retry lands ok=1."""
    seed(conn, "Senior Data Engineer")
    client = FakeClient({"Senior Data Engineer": 8},
                        malformed_first_for={"Senior Data Engineer"})
    aid = matcher.ensure_applicant(conn, profile)
    matcher.run(conn, profile, aid, client=client)
    rows = conn.execute("SELECT ok, input_tokens FROM llm_usage"
                        " ORDER BY id").fetchall()
    assert [(r["ok"], r["input_tokens"]) for r in rows] == [(0, 100), (1, 100)]
