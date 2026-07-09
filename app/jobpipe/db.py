"""SQLite storage + data access layer.

Design rules:
- Schema created idempotently on connect; WAL mode.
- Canonical posting identity = canonical_apply_url, falling back to
  company + normalised title + location.
- transition() enforces the legal-transition map and writes an events row
  atomically with the state change.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager

from .config import settings
from .models import LEGAL_TRANSITIONS, PostingDTO, normalise_title

SCHEMA = """
CREATE TABLE IF NOT EXISTS applicants (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    profile_path TEXT NOT NULL,
    profile_yaml TEXT,                      -- DB-stored profile (onboarded users)
    user_ref TEXT UNIQUE,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    ats TEXT NOT NULL,
    board_token TEXT,
    careers_url TEXT,
    priority INTEGER NOT NULL DEFAULT 3,
    cooldown_until TEXT,
    last_polled_at TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS postings (
    id INTEGER PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id),
    source TEXT NOT NULL CHECK (source IN ('ats','builtin','manual')),
    external_id TEXT,
    title TEXT NOT NULL,
    location TEXT,
    remote_policy TEXT,
    department TEXT,
    apply_url TEXT,
    canonical_apply_url TEXT,
    identity_key TEXT NOT NULL UNIQUE,
    description_text TEXT,
    content_hash TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    closed_at TEXT,
    duplicate_of INTEGER REFERENCES postings(id),
    raw_json TEXT
);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    applicant_id INTEGER NOT NULL REFERENCES applicants(id),
    score INTEGER NOT NULL,
    reasons_json TEXT,
    red_flags_json TEXT,
    extracted_questions_json TEXT,
    model TEXT,
    tokens_used INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    applicant_id INTEGER NOT NULL REFERENCES applicants(id),
    state TEXT NOT NULL,
    answers_json TEXT,
    cover_letter_text TEXT,
    resume_variant TEXT,
    review_notes TEXT,
    submitted_at TEXT,
    confirmation_msg_id TEXT,
    screenshot_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (posting_id, applicant_id)
);
CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY,
    application_id INTEGER REFERENCES applications(id),  -- NULL until linked
    outcome_type TEXT NOT NULL CHECK (outcome_type IN
        ('rejected','interview_invite','assessment','offer','withdrawn','other')),
    occurred_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'email',   -- 'email' | 'manual'
    email_subject TEXT,
    email_from TEXT,
    snippet TEXT,                           -- first ~500 chars for context
    company_guess TEXT,                     -- extracted when unlinked
    email_msg_id TEXT,                      -- dedupe forwarded mail
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    application_id INTEGER,
    posting_id INTEGER,
    event_type TEXT NOT NULL,
    payload_json TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS index_companies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    idx TEXT NOT NULL,                      -- 'ftse100' | 'sp500' | 'builtin'
    domain TEXT,
    source_url TEXT,                        -- e.g. Built In company page (website hint)
    careers_url TEXT,
    status TEXT NOT NULL DEFAULT 'new',     -- new|resolved_ats|workday|bespoke|unresolved
    ats TEXT, board_token TEXT,
    workday_host TEXT, workday_tenant TEXT, workday_site TEXT,
    last_checked TEXT, notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_postings_hash ON postings(content_hash);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
"""


def _backfill_user_refs(conn) -> None:
    """Give every applicant a non-sequential random user_ref (URL token)."""
    import secrets
    for row in conn.execute("SELECT id FROM applicants WHERE user_ref IS NULL").fetchall():
        conn.execute("UPDATE applicants SET user_ref = ? WHERE id = ?",
                     (secrets.token_urlsafe(9), row["id"]))
    conn.commit()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # idempotent migration for pre-existing DBs
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    if "last_polled_at" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN last_polled_at TEXT")
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(applicants)")}
    if "user_ref" not in acols:
        conn.execute("ALTER TABLE applicants ADD COLUMN user_ref TEXT")
    if "profile_yaml" not in acols:
        conn.execute("ALTER TABLE applicants ADD COLUMN profile_yaml TEXT")
    pcols = {r["name"] for r in conn.execute("PRAGMA table_info(postings)")}
    if "duplicate_of" not in pcols:
        conn.execute("ALTER TABLE postings ADD COLUMN duplicate_of INTEGER REFERENCES postings(id)")
    icols = {r["name"] for r in conn.execute("PRAGMA table_info(index_companies)")}
    if "source_url" not in icols:
        conn.execute("ALTER TABLE index_companies ADD COLUMN source_url TEXT")
    mcols = {r["name"] for r in conn.execute("PRAGMA table_info(matches)")}
    for col in ("highlights_json", "alignment_json"):
        if col not in mcols:
            conn.execute(f"ALTER TABLE matches ADD COLUMN {col} TEXT NOT NULL DEFAULT '[]'")
    _backfill_user_refs(conn)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection):
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class IllegalTransition(Exception):
    pass


def identity_key(dto: PostingDTO) -> str:
    if dto.canonical_apply_url:
        return dto.canonical_apply_url
    return f"{dto.company_name.lower()}::{normalise_title(dto.title)}::{(dto.location or '').lower()}"


def get_or_create_company(conn, name: str, ats: str = "custom", **kw) -> int:
    row = conn.execute("SELECT id FROM companies WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO companies (name, ats, board_token, careers_url, priority, notes) "
        "VALUES (?,?,?,?,?,?)",
        (name, ats, kw.get("board_token"), kw.get("careers_url"),
         kw.get("priority", 3), kw.get("notes")),
    )
    return cur.lastrowid


def upsert_posting(conn, dto: PostingDTO, company_id: int | None = None) -> tuple[int, bool]:
    """Insert or refresh a posting. Returns (posting_id, is_new).

    Cross-channel dedup: keyed on identity_key. An existing row is refreshed
    (last_seen_at, and description/hash if changed) rather than duplicated —
    regardless of which source saw it this time.
    """
    key = identity_key(dto)
    ts = now()
    existing = conn.execute("SELECT id, content_hash FROM postings WHERE identity_key = ?", (key,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE postings SET last_seen_at = ?, closed_at = NULL, "
            "description_text = ?, content_hash = ? WHERE id = ?",
            (ts, dto.description_text, dto.hash, existing["id"]),
        )
        return existing["id"], False
    if company_id is None:
        company_id = get_or_create_company(conn, dto.company_name)
    cur = conn.execute(
        "INSERT INTO postings (company_id, source, external_id, title, location, remote_policy,"
        " department, apply_url, canonical_apply_url, identity_key, description_text,"
        " content_hash, first_seen_at, last_seen_at, raw_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (company_id, dto.source, dto.external_id, dto.title, dto.location, dto.remote_policy,
         dto.department, dto.apply_url, dto.canonical_apply_url, key, dto.description_text,
         dto.hash, ts, ts, json.dumps(dto.raw)[:20000]),
    )
    pid = cur.lastrowid
    log_event(conn, event_type="posting_discovered", posting_id=pid,
              payload={"source": dto.source, "title": dto.title})
    return pid, True


def log_event(conn, event_type: str, application_id: int | None = None,
              posting_id: int | None = None, payload: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO events (application_id, posting_id, event_type, payload_json, created_at)"
        " VALUES (?,?,?,?,?)",
        (application_id, posting_id, event_type, json.dumps(payload or {}), now()),
    )


def transition(conn, application_id: int, new_state: str, payload: dict | None = None) -> None:
    """Move an application to new_state, enforcing LEGAL_TRANSITIONS, and write
    the event in the same transaction."""
    row = conn.execute("SELECT state FROM applications WHERE id = ?", (application_id,)).fetchone()
    if row is None:
        raise IllegalTransition(f"application {application_id} does not exist")
    current = row["state"]
    if new_state not in LEGAL_TRANSITIONS.get(current, set()):
        raise IllegalTransition(f"{current} -> {new_state} is not a legal transition")
    with tx(conn):
        conn.execute(
            "UPDATE applications SET state = ?, updated_at = ? WHERE id = ?",
            (new_state, now(), application_id),
        )
        log_event(conn, event_type=f"state:{new_state}", application_id=application_id,
                  payload=payload)


def heartbeat(conn, job: str, ok: bool = True, detail: str = "") -> None:
    with tx(conn):
        log_event(conn, event_type="heartbeat", payload={"job": job, "ok": ok, "detail": detail})
