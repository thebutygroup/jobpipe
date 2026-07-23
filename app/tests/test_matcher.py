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


GOLDEN = {
    "Forward Deployed Engineer": 9,   # obvious fit
    "Senior Data Engineer": 8,        # obvious fit
    "Data Analyst": 2,                # obvious reject
    "Marketing Lead": 1,              # obvious reject
    "Engineering Manager": 6,         # borderline -> below threshold 7
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
