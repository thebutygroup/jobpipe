"""Pipeline health: turns heartbeat events into a per-job status board.

Lesson learned the hard way (July 2026): the matcher catches per-posting
failures so one bad posting can't kill a run — which meant a run where EVERY
call failed (invalid API key) still completed and beat `ok: true` for four
days. Health therefore judges each job on three axes:

  1. honesty  — did the last run report ok?
  2. cadence  — did it run recently enough for its schedule? (staleness)
  3. substance — did the work INSIDE the run succeed? Parsed out of the
     heartbeat detail's stats: a run whose calls all failed is DOWN no
     matter what the heartbeat says.
"""

from __future__ import annotations

import ast
import calendar
import json
import time

from .config import settings

# Expected run interval per scheduled job, in hours (see scheduler.py).
JOB_CADENCE_H = {
    "poll": 12,
    "prefilter": 12,
    "match": 24,
    "prepare": 24,
    "publish": 24,
    "track": 1,
    "indexscan": 24 * 7,
    "digest": 24 * 7,
}
# A job is stale when its last heartbeat is older than cadence + grace.
# Default grace = half the cadence, floor 2h (cron drift, long runs).
_GRACE_FLOOR_H = 2

JOB_BLURBS = {
    "poll": "fetch new postings from every source",
    "prefilter": "cheap rule-based cut before the model sees anything",
    "match": "model scores each posting against each profile",
    "prepare": "extract application form fields for matched postings",
    "publish": "daily digest email",
    "track": "read the inbox for confirmations & outcomes",
    "indexscan": "weekly discovery of new company career pages",
    "digest": "weekly 'new since last time' email to users",
}

DOWN, WARN, OK, OFF = "down", "warn", "ok", "off"


def parse_detail(detail: str | None) -> dict | None:
    """Heartbeat detail is str(stats) — a Python-repr dict (occasionally
    JSON, e.g. publish). Returns the dict or None if it isn't one."""
    if not detail or not detail.strip().startswith("{"):
        return None
    for parser in (ast.literal_eval, json.loads):
        try:
            out = parser(detail)
        except (ValueError, SyntaxError):
            continue
        if isinstance(out, dict):
            return out
    return None


def sum_key(obj, key: str) -> tuple[int, bool]:
    """Recursively sum integer values of `key` in nested dicts — matcher and
    poller stats are keyed per applicant / per source. Returns (total, found)."""
    total, found = 0, False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key and isinstance(v, (int, float)) and not isinstance(v, bool):
                total += int(v)
                found = True
            elif isinstance(v, dict):
                t, f = sum_key(v, key)
                total += t
                found = found or f
    return total, found


def all_calls_failed(stats_by_user: dict) -> tuple[bool, int]:
    """True when a matcher run considered postings and every call failed —
    the exact shape of the invalid-API-key incident. Returns (all_failed, n)."""
    considered, _ = sum_key(stats_by_user, "considered")
    failed, _ = sum_key(stats_by_user, "failed")
    return (considered > 0 and failed >= considered), considered


def substance(detail_obj: dict | None) -> tuple[str, str]:
    """Judge the work inside an ok run. Returns (status, note) where status
    is OK/WARN/DOWN. Generic across jobs: any stats dict carrying
    considered/failed (match) or errors (poll) gets checked."""
    if not detail_obj:
        return OK, ""
    considered, has_c = sum_key(detail_obj, "considered")
    failed, has_f = sum_key(detail_obj, "failed")
    if has_c and has_f and considered > 0:
        if failed >= considered:
            return DOWN, (f"run completed but ALL {considered} calls failed — "
                          f"check the scheduler logs (bad API key?)")
        if failed * 2 >= considered:
            return WARN, f"{failed}/{considered} calls failed"
    elif has_f and failed > 0:
        return WARN, f"{failed} failures in last run"
    errors, has_e = sum_key(detail_obj, "errors")
    if has_e and errors > 0:
        return WARN, f"{errors} source errors in last run"
    return OK, ""


def _age_hours(ts: str, now_utc: str) -> float:
    fmt = "%Y-%m-%dT%H:%M:%S"
    then = calendar.timegm(time.strptime(ts[:19], fmt))
    ref = calendar.timegm(time.strptime(now_utc[:19], fmt))
    return max(0.0, (ref - then) / 3600.0)


def job_board(conn, now_utc: str | None = None) -> list[dict]:
    """One row per scheduled job: last heartbeat, age, and a verdict that a
    glance can trust. Green here MEANS the pipeline is producing."""
    if now_utc is None:
        now_utc = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    rows = conn.execute(
        "SELECT json_extract(payload_json,'$.job') AS job,"
        "       json_extract(payload_json,'$.ok') AS ok,"
        "       json_extract(payload_json,'$.detail') AS detail,"
        "       MAX(created_at) AS last_at"
        " FROM events WHERE event_type='heartbeat'"
        " GROUP BY job").fetchall()
    latest = {r["job"]: r for r in rows}
    board = []
    for job, cadence_h in JOB_CADENCE_H.items():
        entry = {"job": job, "blurb": JOB_BLURBS.get(job, ""),
                 "last_at": None, "age_h": None, "status": DOWN, "note": ""}
        if job == "track" and not settings.track_enabled:
            entry.update(status=OFF, note="disabled (track_enabled=false)")
            board.append(entry)
            continue
        r = latest.get(job)
        if r is None:
            entry["note"] = "never ran"
            board.append(entry)
            continue
        entry["last_at"] = r["last_at"]
        entry["age_h"] = round(_age_hours(r["last_at"], now_utc), 1)
        grace = max(cadence_h / 2.0, _GRACE_FLOOR_H)
        if not r["ok"]:
            entry.update(status=DOWN,
                         note=f"last run failed: {(r['detail'] or '')[:200]}")
        elif entry["age_h"] > cadence_h + grace:
            entry.update(status=DOWN,
                         note=f"stale — expected every ~{cadence_h}h, "
                              f"last ran {entry['age_h']}h ago")
        else:
            status, note = substance(parse_detail(r["detail"]))
            entry.update(status=status, note=note)
        board.append(entry)
    return board


def match_activity(conn, days: int = 14) -> list[dict]:
    """Daily matcher outcomes — the two curves that diverge when the matcher
    silently dies: successes drop to zero while failures pile up."""
    rows = conn.execute(
        "SELECT date(created_at) AS d,"
        "  SUM(event_type='match:MATCHED') AS matched,"
        "  SUM(event_type='match:REJECTED_AUTO') AS rejected,"
        "  SUM(event_type='match:FAILED') AS failed"
        " FROM events WHERE event_type LIKE 'match:%'"
        "  AND created_at >= date('now', ?)"
        " GROUP BY d ORDER BY d DESC", (f"-{days} days",)).fetchall()
    return [dict(r) for r in rows]


def overall(board: list[dict]) -> str:
    """The one-word answer: worst status on the board (off doesn't count)."""
    statuses = {e["status"] for e in board}
    if DOWN in statuses:
        return DOWN
    if WARN in statuses:
        return WARN
    return OK
