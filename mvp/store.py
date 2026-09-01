"""SQLite storage for the MVP.

The real system uses PostgreSQL with pgvector. Here one file holds everything, so the
demo can be copied to another machine and just run.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_tests (
    id              INTEGER PRIMARY KEY,
    test_code       TEXT UNIQUE NOT NULL,
    test_description TEXT NOT NULL,
    exception_code  TEXT,
    exception_description TEXT,
    strata          TEXT,
    department      TEXT,
    risk_rating     TEXT,
    source_reference TEXT,
    is_active       INTEGER NOT NULL DEFAULT 1,
    embedding       TEXT               -- JSON list of floats
);

CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY,
    filename        TEXT NOT NULL,
    source          TEXT NOT NULL,     -- folder | email-body | email-attachment
    source_detail   TEXT,              -- subject / sender / path
    file_hash       TEXT NOT NULL,
    title           TEXT,
    doc_date        TEXT,
    pages           INTEGER,
    status          TEXT NOT NULL,     -- ingested | duplicate | error
    duplicate_of    INTEGER,
    ingested_at     TEXT NOT NULL,
    -- A document can arrive inside another document — ABL's own BRD carries its
    -- annexures as embedded files. The child records where it came from so a reviewer
    -- can see "Annexure B, inside BPRD Circular 07".
    parent_id       INTEGER,
    kind            TEXT,              -- native-pdf | mixed-pdf | docx | excel | image …
    ocr_pages       INTEGER NOT NULL DEFAULT 0,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS clauses (
    id              INTEGER PRIMARY KEY,
    document_id     INTEGER NOT NULL,
    clause_ref      TEXT,
    sequence        INTEGER NOT NULL,
    text            TEXT NOT NULL,
    page_number     INTEGER,
    char_start      INTEGER,
    char_end        INTEGER,
    is_actionable   INTEGER,           -- 1 / 0 / NULL = not judged
    strata_tag      TEXT,
    reason          TEXT
);

CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY,
    clause_id       INTEGER NOT NULL,
    document_id     INTEGER NOT NULL,
    sr_no           TEXT,
    change_type     TEXT NOT NULL,
    amendment_type  TEXT,
    target_test_code TEXT,
    existing_test_description TEXT,
    proposed_test_description TEXT,
    exception_code  TEXT,
    proposed_exception_description TEXT,
    strata          TEXT,
    department      TEXT,
    risk_rating     TEXT,
    root_cause      TEXT,
    rationale       TEXT,
    confidence      REAL,
    candidates      TEXT,              -- JSON: the shortlist that was considered
    -- Review workflow. The proposal's flow is intake -> AI analysis -> Excel working
    -- file -> HUMAN REVIEW -> eAudit, so the demo has to show the review step too.
    status          TEXT NOT NULL DEFAULT 'Pending review',
    current_level   INTEGER NOT NULL DEFAULT 1,   -- 1 = reviewer, 2 = approver
    reviewer_note   TEXT,
    approver_note   TEXT
);

CREATE TABLE IF NOT EXISTS approvals (
    id              INTEGER PRIMARY KEY,
    proposal_id     INTEGER NOT NULL,
    level           INTEGER NOT NULL,
    decision        TEXT NOT NULL,     -- Approved | Rejected | Changes requested
    note            TEXT,
    decided_at      TEXT NOT NULL
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init(reset: bool = False) -> None:
    if reset and Path(config.DB_PATH).exists():
        Path(config.DB_PATH).unlink()
    with connect() as conn:
        conn.executescript(SCHEMA)


def insert(conn: sqlite3.Connection, table: str, row: dict) -> int:
    keys = list(row)
    placeholders = ", ".join("?" for _ in keys)
    sql = f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({placeholders})"
    cur = conn.execute(sql, [row[k] for k in keys])
    return cur.lastrowid


def insert_many(conn: sqlite3.Connection, table: str, rows: Iterable[dict]) -> int:
    count = 0
    for row in rows:
        insert(conn, table, row)
        count += 1
    return count


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> Optional[dict]:
    rows = query(sql, params)
    return rows[0] if rows else None


def scalar(sql: str, params: tuple = ()) -> Any:
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None


def load_library() -> list[dict]:
    """Every active test, with its embedding decoded."""
    rows = query("SELECT * FROM audit_tests WHERE is_active = 1 ORDER BY id")
    for row in rows:
        row["embedding"] = json.loads(row["embedding"]) if row["embedding"] else []
    return rows


def approval_counts() -> dict:
    rows = query("SELECT status, COUNT(*) n FROM proposals GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def counts() -> dict:
    return {
        "tests": scalar("SELECT COUNT(*) FROM audit_tests") or 0,
        "documents": scalar("SELECT COUNT(*) FROM documents WHERE status != 'duplicate'") or 0,
        "duplicates": scalar("SELECT COUNT(*) FROM documents WHERE status = 'duplicate'") or 0,
        "nested": scalar("SELECT COUNT(*) FROM documents WHERE parent_id IS NOT NULL") or 0,
        "failed": scalar("SELECT COUNT(*) FROM documents WHERE status = 'error'") or 0,
        "ocr_pages": scalar("SELECT COALESCE(SUM(ocr_pages), 0) FROM documents") or 0,
        "clauses": scalar("SELECT COUNT(*) FROM clauses") or 0,
        "actionable": scalar("SELECT COUNT(*) FROM clauses WHERE is_actionable = 1") or 0,
        "proposals": scalar("SELECT COUNT(*) FROM proposals") or 0,
        "approved": scalar("SELECT COUNT(*) FROM proposals WHERE status = 'Approved'") or 0,
        "pending": scalar("SELECT COUNT(*) FROM proposals "
                          "WHERE status NOT IN ('Approved', 'Rejected')") or 0,
    }
