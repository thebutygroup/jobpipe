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
    source TEXT NOT NULL,               -- validated against the source registry in code

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
CREATE TABLE IF NOT EXISTS apply_routes (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL UNIQUE REFERENCES postings(id),
    platform TEXT NOT NULL,             -- greenhouse|lever|ashby|workable|company_site|login_walled
    method TEXT NOT NULL,               -- browser_form | manual_assist
    final_url TEXT NOT NULL,
    hops_json TEXT,                     -- resolution chain, for audit
    resolved_at TEXT NOT NULL,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY,
    applicant_id INTEGER NOT NULL REFERENCES applicants(id),
    kind TEXT NOT NULL DEFAULT 'resume',
    variant_name TEXT NOT NULL DEFAULT 'default',
    filename TEXT NOT NULL,             -- vault-generated on-disk name
    original_name TEXT,
    content_sha256 TEXT,
    size INTEGER,
    uploaded_at TEXT NOT NULL,
    UNIQUE (applicant_id, kind, variant_name)
);
CREATE TABLE IF NOT EXISTS source_postings (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    source TEXT NOT NULL,               -- specific adapter: greenhouse|lever|adzuna|...
    external_id TEXT,
    url TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_postings_hash ON postings(content_hash);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_sp_posting ON source_postings(posting_id);
CREATE INDEX IF NOT EXISTS idx_sp_source_ext ON source_postings(source, external_id);
-- hot-path indexes (added after the matcher grew per-posting cap checks and
-- per-posting EXISTS probes into events/matches; without these every probe is
-- a full scan — brutal on a Windows-bind-mounted SQLite file):
CREATE INDEX IF NOT EXISTS idx_events_posting_type ON events(posting_id, event_type);
CREATE INDEX IF NOT EXISTS idx_events_type_created ON events(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_matches_applicant ON matches(applicant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_matches_posting_app ON matches(posting_id, applicant_id);
CREATE INDEX IF NOT EXISTS idx_matches_created ON matches(created_at);
"""


def _migrate_postings_source_check(conn) -> None:
    """Pre-multi-source DBs constrain postings.source with a CHECK
    ('ats','builtin','manual'), which rejects new sources. SQLite cannot drop
    a CHECK in place, so rebuild the table once (SQLite ALTER TABLE recipe:
    FK off -> new table -> copy -> drop -> rename -> FK check)."""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' "
                       "AND name='postings'").fetchone()
    if not row or "CHECK (source IN" not in (row["sql"] or ""):
        return
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(postings)")]
        col_list = ", ".join(cols)
        new_sql = row["sql"].replace(
            "source TEXT NOT NULL CHECK (source IN ('ats','builtin','manual'))",
            "source TEXT NOT NULL").replace(
            "CREATE TABLE postings", "CREATE TABLE postings_new")
        if "postings_new" not in new_sql or "CHECK (source IN" in new_sql:
            raise RuntimeError("postings CHECK migration: unexpected table SQL; "
                               "refusing to guess")
        conn.executescript(f"""
            {new_sql};
            INSERT INTO postings_new ({col_list}) SELECT {col_list} FROM postings;
            DROP TABLE postings;
            ALTER TABLE postings_new RENAME TO postings;
            CREATE INDEX IF NOT EXISTS idx_postings_hash ON postings(content_hash);
        """)
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise RuntimeError(f"postings CHECK migration broke FKs: {bad[:3]}")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _backfill_user_refs(conn) -> None:
    """Give every applicant a non-sequential random user_ref (URL token)."""
    import secrets
    for row in conn.execute("SELECT id FROM applicants WHERE user_ref IS NULL").fetchall():
        conn.execute("UPDATE applicants SET user_ref = ? WHERE id = ?",
                     (secrets.token_urlsafe(9), row["id"]))
    conn.commit()


def now() -> str:
    """UTC, always. Naive local time breaks daily caps and submission
    intervals the moment the process TZ differs from expectations (Django
    exports TIME_ZONE to the whole process). SQLite's date('now') is UTC,
    so storing UTC makes every comparison consistent."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def connect(db_path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path or settings.db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    # idempotent migrations for pre-existing DBs
    _migrate_postings_source_check(conn)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    if "last_polled_at" not in cols:
        conn.execute("ALTER TABLE companies ADD COLUMN last_polled_at TEXT")
    acols = {r["name"] for r in conn.execute("PRAGMA table_info(applicants)")}
    if "user_ref" not in acols:
        conn.execute("ALTER TABLE applicants ADD COLUMN user_ref TEXT")
    if "profile_yaml" not in acols:
        conn.execute("ALTER TABLE applicants ADD COLUMN profile_yaml TEXT")
    if "vault_token" not in acols:
        conn.execute("ALTER TABLE applicants ADD COLUMN vault_token TEXT")
    if "edit_token" not in acols:
        # secret in the profile-edit URL (emailed to the user; unguessable)
        conn.execute("ALTER TABLE applicants ADD COLUMN edit_token TEXT")
    if "shadow_banned" not in acols:
        # 1 = pipeline silently ignores them (matching, searches, emails);
        # their pages still render so nothing looks different from outside
        conn.execute("ALTER TABLE applicants ADD COLUMN shadow_banned INTEGER NOT NULL DEFAULT 0")
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


def record_provenance(conn, posting_id: int, dto: PostingDTO) -> None:
    """Upsert this sighting into source_postings: which source saw this job,
    under what ID/URL, first/last seen. Idempotent per (source, external_id)."""
    src = dto.source_detail or dto.source
    ts = now()
    if dto.external_id:
        row = conn.execute("SELECT id FROM source_postings WHERE source = ? "
                           "AND external_id = ?", (src, dto.external_id)).fetchone()
    else:
        row = conn.execute("SELECT id FROM source_postings WHERE source = ? "
                           "AND posting_id = ? AND url = ?",
                           (src, posting_id, dto.apply_url)).fetchone()
    if row:
        conn.execute("UPDATE source_postings SET last_seen_at = ? WHERE id = ?",
                     (ts, row["id"]))
        return
    conn.execute(
        "INSERT INTO source_postings (posting_id, source, external_id, url,"
        " first_seen_at, last_seen_at, raw_json) VALUES (?,?,?,?,?,?,?)",
        (posting_id, src, dto.external_id, dto.apply_url, ts, ts,
         json.dumps(dto.raw)[:20000]))


def upsert_posting(conn, dto: PostingDTO, company_id: int | None = None) -> tuple[int, bool]:
    """Insert or refresh a posting. Returns (posting_id, is_new).

    Identity resolution, in order:
      1. exact identity_key (canonical apply URL) hit -> refresh
      2. dedupe.find_canonical: same company + same/fuzzy title + compatible
         location -> attach this sighting to the existing canonical posting
         (and promote it in place if this source is preferred, e.g. ATS over
         an aggregator)
      3. otherwise insert a new canonical posting
    Every path records provenance in source_postings, so re-running a poll
    never duplicates rows and source analytics can always answer "who saw
    this job, and when".
    """
    from . import dedupe

    key = identity_key(dto)
    ts = now()
    existing = conn.execute("SELECT id, content_hash FROM postings WHERE identity_key = ?", (key,)).fetchone()
    if existing:
        conn.execute(
            "UPDATE postings SET last_seen_at = ?, closed_at = NULL, "
            "description_text = ?, content_hash = ? WHERE id = ?",
            (ts, dto.description_text, dto.hash, existing["id"]),
        )
        record_provenance(conn, existing["id"], dto)
        return existing["id"], False

    canonical_id, explanation = dedupe.find_canonical(conn, dto)
    if canonical_id is not None:
        canonical = conn.execute("SELECT id, source, description_text FROM postings "
                                 "WHERE id = ?", (canonical_id,)).fetchone()
        conn.execute("UPDATE postings SET last_seen_at = ?, closed_at = NULL WHERE id = ?",
                     (ts, canonical_id))
        # Longer description wins (aggregators carry snippets, ATS the full text).
        if len(dto.description_text or "") > len(canonical["description_text"] or ""):
            conn.execute("UPDATE postings SET description_text = ?, content_hash = ? "
                         "WHERE id = ?", (dto.description_text, dto.hash, canonical_id))
        # Preferred source arriving later promotes the canonical record in place.
        if dedupe.source_rank(dto.source) < dedupe.source_rank(canonical["source"]):
            conn.execute(
                "UPDATE postings SET source = ?, apply_url = ?, canonical_apply_url = ?, "
                "identity_key = ?, external_id = ? WHERE id = ?",
                (dto.source, dto.apply_url, dto.canonical_apply_url, key,
                 dto.external_id, canonical_id))
            explanation["promoted_to"] = dto.source
        record_provenance(conn, canonical_id, dto)
        log_event(conn, event_type="dedupe_linked", posting_id=canonical_id,
                  payload=explanation)
        return canonical_id, False

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
    record_provenance(conn, pid, dto)
    log_event(conn, event_type="posting_discovered", posting_id=pid,
              payload={"source": dto.source_detail or dto.source, "title": dto.title})
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
