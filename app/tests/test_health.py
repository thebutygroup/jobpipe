"""Pipeline health board — born from the July 2026 incident where the matcher
ran for four days with every call failing (invalid API key) while its
heartbeat reported ok=true. These tests pin the three axes: honesty (ok flag),
cadence (staleness) and substance (all-calls-failed detection)."""

import os

import django
from django.test import Client

from jobpipe import health
from jobpipe.db import heartbeat, log_event


def setup_module(module):
    os.environ["JOBPIPE_TESTING"] = "1"
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "jobpipe.dashboard.settings")
    django.setup()


NOW = "2026-07-27T12:00:00"

MATCH_ALL_FAILED = ("{'Joe Buty': {'considered': 229, 'matched': 0, 'rejected': 0,"
                    " 'failed': 229, 'capped': 0, 'tokens': 0}, 'Eeezee':"
                    " {'considered': 434, 'matched': 0, 'rejected': 0,"
                    " 'failed': 434, 'capped': 0, 'tokens': 0}}")
MATCH_HEALTHY = ("{'Joe Buty': {'considered': 9, 'matched': 9, 'rejected': 0,"
                 " 'failed': 0, 'capped': 0, 'tokens': 25771}}")


def _beat(conn, job, ok, detail, at):
    heartbeat(conn, job, ok=ok, detail=detail)
    conn.execute("UPDATE events SET created_at = ? WHERE id ="
                 " (SELECT MAX(id) FROM events)", (at,))
    conn.commit()


def _row(board, job):
    return next(e for e in board if e["job"] == job)


# ---- parsing ----------------------------------------------------------------

def test_parse_detail_python_repr_and_json():
    assert health.parse_detail(MATCH_HEALTHY)["Joe Buty"]["matched"] == 9
    assert health.parse_detail('{"pending": 3, "sent": 1}') == {"pending": 3, "sent": 1}
    assert health.parse_detail("confirmed=0 outcomes=0") is None
    assert health.parse_detail("") is None
    assert health.parse_detail(None) is None


def test_sum_key_recurses_per_user_stats():
    obj = health.parse_detail(MATCH_ALL_FAILED)
    assert health.sum_key(obj, "considered") == (663, True)
    assert health.sum_key(obj, "failed") == (663, True)
    assert health.sum_key({"a": 1}, "missing") == (0, False)


# ---- substance: the silent-failure detector ---------------------------------

def test_all_calls_failed_flags_the_incident_shape():
    all_failed, n = health.all_calls_failed(health.parse_detail(MATCH_ALL_FAILED))
    assert all_failed and n == 663
    ok_run, _ = health.all_calls_failed(health.parse_detail(MATCH_HEALTHY))
    assert not ok_run
    assert health.all_calls_failed({}) == (False, 0)


def test_substance_down_when_all_fail_warn_when_half():
    assert health.substance(health.parse_detail(MATCH_ALL_FAILED))[0] == health.DOWN
    status, note = health.substance({"u": {"considered": 10, "failed": 6}})
    assert status == health.WARN and "6/10" in note
    assert health.substance({"u": {"considered": 10, "failed": 1}})[0] == health.OK
    assert health.substance(None)[0] == health.OK
    # poller shape: errors > 0 warns
    assert health.substance({"ats": {"new": 5, "errors": 2}})[0] == health.WARN


# ---- the board --------------------------------------------------------------

def test_board_red_despite_ok_heartbeat(conn):
    """The incident, replayed: heartbeat ok=True, every call failed => DOWN."""
    _beat(conn, "match", True, MATCH_ALL_FAILED, "2026-07-27T05:52:58")
    row = _row(health.job_board(conn, now_utc=NOW), "match")
    assert row["status"] == health.DOWN
    assert "ALL 663 calls failed" in row["note"]


def test_board_green_on_healthy_run(conn):
    _beat(conn, "match", True, MATCH_HEALTHY, "2026-07-27T05:45:57")
    row = _row(health.job_board(conn, now_utc=NOW), "match")
    assert row["status"] == health.OK and row["age_h"] < 7


def test_board_stale_and_never_ran(conn):
    _beat(conn, "match", True, MATCH_HEALTHY, "2026-07-23T05:45:57")  # 4 days old
    board = health.job_board(conn, now_utc=NOW)
    assert _row(board, "match")["status"] == health.DOWN
    assert "stale" in _row(board, "match")["note"]
    assert _row(board, "poll")["status"] == health.DOWN
    assert _row(board, "poll")["note"] == "never ran"


def test_board_honours_explicit_failure_and_overall(conn):
    _beat(conn, "poll", False, "FetchError: boom", "2026-07-27T06:00:00")
    board = health.job_board(conn, now_utc=NOW)
    assert _row(board, "poll")["status"] == health.DOWN
    assert "boom" in _row(board, "poll")["note"]
    assert health.overall(board) == health.DOWN


def test_track_off_when_disabled(conn, monkeypatch):
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "track_enabled", False)
    row = _row(health.job_board(conn, now_utc=NOW), "track")
    assert row["status"] == health.OFF


def test_match_activity_counts_by_day(conn):
    for et in ("match:MATCHED", "match:MATCHED", "match:FAILED"):
        log_event(conn, et, posting_id=None)
    conn.commit()
    days = health.match_activity(conn)
    assert days and days[0]["matched"] == 2 and days[0]["failed"] == 1


# ---- matcher heartbeat integration ------------------------------------------

def test_matcher_finish_run_beats_red_and_alerts(conn, monkeypatch):
    """The real code path: matcher.finish_run beats ok=False and emails Joe
    when every call in the run failed; beats ok=True (no email) otherwise."""
    from jobpipe.matching import matcher

    sent = []
    monkeypatch.setattr(matcher, "send_failure",
                        lambda job, detail: sent.append((job, detail)))
    matcher.finish_run(conn, {"Joe": {"considered": 5, "matched": 0,
                                      "rejected": 0, "failed": 5,
                                      "capped": 0, "tokens": 0}})
    conn.commit()
    row = conn.execute(
        "SELECT json_extract(payload_json,'$.ok') AS ok FROM events"
        " WHERE event_type='heartbeat' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["ok"] == 0
    assert len(sent) == 1 and sent[0][0] == "match"
    assert "ALL 5 model calls failed" in sent[0][1]

    matcher.finish_run(conn, {"Joe": {"considered": 5, "matched": 4,
                                      "rejected": 1, "failed": 0,
                                      "capped": 0, "tokens": 900}})
    conn.commit()
    row = conn.execute(
        "SELECT json_extract(payload_json,'$.ok') AS ok FROM events"
        " WHERE event_type='heartbeat' ORDER BY id DESC LIMIT 1").fetchone()
    assert row["ok"] == 1
    assert len(sent) == 1  # no new alert


# ---- the page ---------------------------------------------------------------

def test_health_page_renders(conn, monkeypatch):
    from jobpipe.config import settings
    monkeypatch.setattr(settings, "db_path",
                        conn.execute("PRAGMA database_list").fetchone()["file"])
    _beat(conn, "match", True, MATCH_ALL_FAILED, "2026-07-27T05:52:58")
    r = Client().get("/health")
    assert r.status_code == 200
    assert b"Pipeline health" in r.content
    assert b"Attention needed" in r.content or b"down" in r.content
