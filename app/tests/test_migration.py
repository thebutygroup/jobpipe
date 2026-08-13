"""The postings.source CHECK-constraint rebuild migration must preserve data
and unlock new source values on a pre-multi-source database."""

import sqlite3

from jobpipe import db as dbmod

OLD_SCHEMA = """
CREATE TABLE companies (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, ats TEXT NOT NULL,
    board_token TEXT, careers_url TEXT, priority INTEGER NOT NULL DEFAULT 3,
    cooldown_until TEXT, last_polled_at TEXT, notes TEXT);
CREATE TABLE postings (
    id INTEGER PRIMARY KEY,
    company_id INTEGER REFERENCES companies(id),
    source TEXT NOT NULL CHECK (source IN ('ats','builtin','manual')),
    external_id TEXT, title TEXT NOT NULL, location TEXT, remote_policy TEXT,
    department TEXT, apply_url TEXT, canonical_apply_url TEXT,
    identity_key TEXT NOT NULL UNIQUE, description_text TEXT, content_hash TEXT,
    first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, closed_at TEXT,
    raw_json TEXT);
CREATE TABLE applications (
    id INTEGER PRIMARY KEY,
    posting_id INTEGER NOT NULL REFERENCES postings(id),
    applicant_id INTEGER NOT NULL, state TEXT NOT NULL,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


def test_check_constraint_migration(tmp_path):
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.execute("INSERT INTO companies (name, ats) VALUES ('Acme', 'greenhouse')")
    raw.execute("INSERT INTO postings (company_id, source, title, identity_key,"
                " first_seen_at, last_seen_at) VALUES (1, 'ats', 'SDE', 'k1',"
                " datetime('now'), datetime('now'))")
    raw.execute("INSERT INTO applications (posting_id, applicant_id, state,"
                " created_at, updated_at) VALUES (1, 1, 'MATCHED',"
                " datetime('now'), datetime('now'))")
    raw.commit()
    # old schema really does reject a new source value
    try:
        raw.execute("INSERT INTO postings (company_id, source, title, identity_key,"
                    " first_seen_at, last_seen_at) VALUES (1, 'adzuna', 'X', 'k2',"
                    " datetime('now'), datetime('now'))")
        raise AssertionError("expected IntegrityError from old CHECK")
    except sqlite3.IntegrityError:
        pass
    raw.close()

    conn = dbmod.connect(path)
    # data survived, FK from applications intact
    assert conn.execute("SELECT title FROM postings WHERE id=1").fetchone()["title"] == "SDE"
    assert conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"] == 1
    assert not conn.execute("PRAGMA foreign_key_check").fetchall()
    # new sources now insert fine
    conn.execute("INSERT INTO postings (company_id, source, title, identity_key,"
                 " first_seen_at, last_seen_at) VALUES (1, 'adzuna', 'X', 'k2',"
                 " datetime('now'), datetime('now'))")
    conn.commit()
    # migration is idempotent: reconnecting doesn't rebuild or lose anything
    conn.close()
    conn2 = dbmod.connect(path)
    assert conn2.execute("SELECT COUNT(*) c FROM postings").fetchone()["c"] == 2
    conn2.close()


def test_llm_usage_table_added_to_preexisting_db(tmp_path):
    """Opening an existing DB (created before llm_usage existed) grows the
    table idempotently — twice in a row is fine."""
    path = str(tmp_path / "old.db")
    raw = sqlite3.connect(path)
    raw.executescript(OLD_SCHEMA)
    raw.close()
    for _ in (1, 2):
        c = dbmod.connect(path)
        cols = {r[1] for r in c.execute("PRAGMA table_info(llm_usage)")}
        assert {"created_at", "applicant_id", "posting_id", "model",
                "input_tokens", "output_tokens", "cache_read_tokens",
                "batch_id", "ok"} <= cols
        dbmod.record_llm_usage(c, "test-model", {"input": 1, "output": 2},
                               ok=False)
        c.commit()
        c.close()
