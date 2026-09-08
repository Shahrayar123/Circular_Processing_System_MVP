"""SQLite storage for the MVP.

The real system uses PostgreSQL with pgvector. Here one file holds everything, so the
demo can be copied to another machine and just run.
"""

import json
import sqlite3
from datetime import datetime
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
    -- Set when the clause split looks wrong — one clause for a long document, hundreds of
    -- fragments, and so on. It FLAGS the document; it never fails it.
    segment_warning TEXT,
    ocr_pages       INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    -- Set ONLY when every stage finished for this document. A document row is written at
    -- intake, before it is split into clauses, so the row existing proves nothing: if the
    -- run dies in between, the row is there with no clauses behind it. Deciding "already
    -- processed" from the row alone would then skip that document on every future run and
    -- lose the circular silently — the worst failure this system has.
    --
    -- An explicit completion marker, rather than inferring completeness from a clause
    -- count, because a document that genuinely yields zero clauses is finished, and a
    -- document that crashed during splitting is not. The two are indistinguishable by
    -- counting.
    processed_at    TEXT,
    -- Reissues. A circular is routinely sent again with clauses added or corrected, under
    -- the SAME filename. The content hash differs, so it is correctly re-read as a new
    -- document — but without this link the two rows sit side by side with nothing saying
    -- one replaces the other, and a reviewer sees every unchanged clause twice as two
    -- independent proposals. Approving both is then the natural thing to do.
    supersedes      INTEGER,   -- on the NEW row: the document it replaces
    superseded_by   INTEGER    -- on the OLD row: the document that replaced it
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
    reason          TEXT,
    -- Which rung of the splitting ladder produced this clause: structure | paragraph |
    -- obligation-lines. Patterns cannot cover every circular, and a splitter that fell
    -- back must say so — otherwise a bad split is indistinguishable from a good one.
    segmented_by    TEXT,
    -- Set when the text sent to a model had to be shortened to fit. The clause itself is
    -- stored WHOLE; only the model input is capped, and never silently.
    model_input_truncated INTEGER NOT NULL DEFAULT 0,
    -- Which judge ruled on this clause: "llm", "rules", or "rules (model unavailable)".
    -- Stored per clause, not per run: a single circular can be judged by the model for
    -- most of its clauses and fall back for one batch, and the reviewer should be able
    -- to see exactly which.
    judged_by       TEXT
);

-- A clause routinely carries SEVERAL duties in one sentence:
--
--   "Banks shall verify every agent through NADRA before onboarding, shall maintain a
--    register reconciled monthly, and shall not permit an agent to operate under more
--    than one code."
--
-- That is three obligations, three different existing tests, and three proposals. Treat
-- the clause as the unit and two of them are lost with no error — and retrieval degrades
-- too, because embedding a compound sentence gives the CENTROID of its meanings, close
-- to none of them.
--
-- The clause TEXT is never split: it stays the unit of traceability, with one reference,
-- one page and one pair of offsets. Obligations are recorded AGAINST it.
CREATE TABLE IF NOT EXISTS obligations (
    id              INTEGER PRIMARY KEY,
    clause_id       INTEGER NOT NULL,
    sequence        INTEGER NOT NULL,  -- 1, 2, 3 within the clause
    text            TEXT NOT NULL,     -- the duty stated on its own — the retrieval query
    strata_tag      TEXT,
    split_by        TEXT               -- rules | llm — who found the obligations
);

CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY,
    clause_id       INTEGER NOT NULL,
    -- Which duty within the clause this proposal acts on. Several proposals share one
    -- clause_id and therefore one source reference, page and offsets.
    obligation_id   INTEGER,
    obligation_index INTEGER NOT NULL DEFAULT 1,
    document_id     INTEGER NOT NULL,
    sr_no           TEXT,
    change_type     TEXT NOT NULL,
    amendment_type  TEXT,
    target_test_code TEXT,
    existing_test_description TEXT,
    proposed_test_description TEXT,
    exception_code  TEXT,
    proposed_exception_description TEXT,
    -- THE "BEFORE", frozen at the moment the decision was taken. The library changes
    -- over time, so a live join back to audit_tests would show today's wording, not the
    -- wording the reviewer actually approved a change against. Every field the proposal
    -- can alter is snapshotted, or "what changed" cannot be answered later.
    existing_exception_code TEXT,
    existing_exception_description TEXT,
    existing_strata TEXT,
    existing_department TEXT,
    existing_risk_rating TEXT,
    existing_source_reference TEXT,
    decided_at      TEXT,
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

-- Every edit to a proposal after the engine produced it, one row per field changed.
-- APPEND ONLY: a row is never updated or deleted, so the question "who changed this
-- wording, from what, to what, and when" always has an answer. Without it the proposal
-- row shows only the latest text and the reviewer's change is indistinguishable from
-- what the system proposed.
CREATE TABLE IF NOT EXISTS proposal_revisions (
    id              INTEGER PRIMARY KEY,
    proposal_id     INTEGER NOT NULL,
    field           TEXT NOT NULL,     -- proposed_test_description | risk_rating | …
    old_value       TEXT,
    new_value       TEXT,
    changed_by      TEXT NOT NULL,     -- reviewer (level 1) | approver (level 2)
    level           INTEGER NOT NULL,
    note            TEXT,
    changed_at      TEXT NOT NULL
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


# ====== CONNECTION AND SCHEMA ======

def connect() -> sqlite3.Connection:
    """Open a connection with row_factory set, so rows come back like dicts.

    Callers use `with connect() as conn`, which COMMITS on exit but does not close —
    that is sqlite3's behaviour, not an oversight. Every helper here opens its own
    connection; the demo is single-user and the simplicity is worth more than pooling.
    """
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def delete_database() -> bool:
    """Delete the database file. Returns False if there was nothing to delete.

    Separated from `init()` so a wipe can happen WITHOUT immediately recreating anything —
    that is what lets `--reset-only` leave a genuinely empty slate for the next run to
    build from, which is what you want when stepping through that run in a debugger.
    """
    path = Path(config.DB_PATH)
    if not path.exists():
        return False
    try:
        path.unlink()
    except PermissionError:
        # Windows will not delete an open file, and Streamlit keeps a connection for as
        # long as the browser tab is open. The bare OS error names neither the cause nor
        # the fix, so it is replaced with one that does.
        raise SystemExit(
            f"cannot delete {config.DB_PATH}: another program has it open.\n"
            "Close the Streamlit app (Ctrl-C in its terminal) and run this again.\n"
            "A run without --reset or --reset-only does not need the file closed.")
    return True


def init(reset: bool = False) -> None:
    """Create the schema if it is not there. With reset=True the database file is DELETED.

    **The default is reset=False, and a normal run never passes True.** The database is
    the only place a reviewer's sign-off, an approver's decision and every wording edit
    exist — SQLite here, PostgreSQL in the live system, but the same rule. A pipeline that
    rebuilds from scratch every time silently throws all of that away, and the second run
    of the week destroys the first week's review work with no error and no warning.

    `--reset` on the command line is the only way to get True, and it is for the developer
    who wants a clean demo, never for a scheduled run.
    """
    if reset:
        delete_database()
    # CREATE TABLE IF NOT EXISTS throughout, so this is safe to call on every run — it
    # adds what is missing and leaves existing rows alone.
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


# Columns added after the first databases were built. CREATE TABLE IF NOT EXISTS does
# NOTHING to a table that already exists, so a schema change here would never reach a
# database someone built last week — and every query using the new column would fail with
# "no such column" on their machine and nowhere else.
#
# Kept as a plain list because the MVP only ever adds nullable columns. Anything more
# (a rename, a type change, a backfill) is the point at which this needs a real migration
# tool rather than one more entry here.
_ADDED_COLUMNS = [
    ("documents", "processed_at", "TEXT"),
    ("documents", "supersedes", "INTEGER"),
    ("documents", "superseded_by", "INTEGER"),
]


def _dedupe_clauses(conn: sqlite3.Connection) -> int:
    """Remove any SECOND set of clauses a document accumulated. Returns rows deleted.

    A document should have exactly one clause per sequence number. A duplicate set means
    segmentation ran twice for it, which the code now prevents — but a database written
    before it did still carries the duplicates, and every one of them is a second copy of
    a proposal in the reviewer's queue.

    The EARLIEST set is kept: its proposals are the ones with the lower Sr numbers, which
    are what anyone looking at the working file has already seen.
    """
    doomed = [r[0] for r in conn.execute(
        "SELECT id FROM clauses WHERE id NOT IN ("
        "  SELECT MIN(id) FROM clauses GROUP BY document_id, sequence)")]
    if not doomed:
        return 0
    marks = ", ".join("?" for _ in doomed)
    conn.execute(f"DELETE FROM obligations WHERE clause_id IN ({marks})", doomed)
    conn.execute(f"DELETE FROM proposals WHERE clause_id IN ({marks})", doomed)
    conn.execute(f"DELETE FROM clauses WHERE id IN ({marks})", doomed)
    print(f"   NOTE: removed {len(doomed)} duplicate clause(s) left by an earlier run, "
          f"with their obligations and proposals.")
    return len(doomed)


def _migrate(conn: sqlite3.Connection) -> None:
    """Add any column missing from an older database. Safe to run on every startup."""
    added = set()
    for table, column, sql_type in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sql_type}")
            added.add(column)

    if "processed_at" in added:
        # A database built before this column existed has no completion marks, so every
        # document in it would look half-processed and be re-read once — throwing away
        # proposals that were perfectly good. Anything with clauses behind it plainly did
        # finish, so say so.
        #
        # This inference is only safe HERE, as a one-off for databases that predate the
        # column. It is not safe as the general rule, which is why `is_complete()` does
        # not use it: from then on, a document with no clauses must mean "unfinished".
        conn.execute(
            "UPDATE documents SET processed_at = ingested_at "
            "WHERE processed_at IS NULL AND ("
            "  status = 'duplicate' "
            "  OR EXISTS (SELECT 1 FROM clauses c WHERE c.document_id = documents.id))")

    # One clause per (document, sequence) — enforced by the DATABASE, not by trusting the
    # code above it. Segmentation running twice for one document produced a second full
    # set of clauses, each with its own proposal, so every row appeared twice in the Word
    # working document with two different Sr numbers. Nothing errored, and nothing on any
    # screen said the document had been segmented twice. Existing duplicates have to go
    # before the index can be created, which is why the clean-up runs first.
    _dedupe_clauses(conn)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS clauses_one_per_sequence "
                 "ON clauses (document_id, sequence)")


def library_loaded() -> bool:
    """True when the audit-test library has been generated AND indexed.

    Both halves matter. A library with rows but no embeddings cannot be searched, and the
    failure shows up much later as every clause matching nothing — so an interrupted
    setup must count as "not loaded" and be redone, not skipped as already present.
    """
    if not ready():
        return False
    return (scalar("SELECT COUNT(*) FROM audit_tests WHERE embedding IS NOT NULL") or 0) > 0


def ready() -> bool:
    """True only when the demo database has actually been BUILT.

    Checking that the file exists is not enough. SQLite creates the file the moment
    anything connects to it, so one stray query before `run_pipeline.py` has ever run
    leaves a real but table-less database behind. From then on a file-existence check
    passes and every query fails with "no such table: audit_tests" — which tells a new
    user nothing about what they actually need to do.
    """
    if not Path(config.DB_PATH).exists():
        return False
    try:
        with connect() as conn:
            return conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name = 'audit_tests'").fetchone() is not None
    except sqlite3.Error:
        return False


# ====== WRITING ======

def insert(conn: sqlite3.Connection, table: str, row: dict) -> int:
    """Insert one row from a dict and return its new id.

    Column names come from the dict keys, so a typo becomes a SQL error rather than a
    silently ignored field.
    """
    keys = list(row)
    placeholders = ", ".join("?" for _ in keys)
    sql = f"INSERT INTO {table} ({', '.join(keys)}) VALUES ({placeholders})"
    cur = conn.execute(sql, [row[k] for k in keys])
    return cur.lastrowid


def insert_many(conn: sqlite3.Connection, table: str, rows: Iterable[dict]) -> int:
    """Insert every row and return how many. One transaction, via the caller's connection."""
    count = 0
    for row in rows:
        insert(conn, table, row)
        count += 1
    return count


# ====== READING ======

def query(sql: str, params: tuple = ()) -> list[dict]:
    """Run a SELECT and return a list of plain dicts. Opens and closes its own connection."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def one(sql: str, params: tuple = ()) -> Optional[dict]:
    """The first row of a SELECT as a dict, or None. Use it where a query cannot match twice."""
    rows = query(sql, params)
    return rows[0] if rows else None


def scalar(sql: str, params: tuple = ()) -> Any:
    """The first column of the first row — a COUNT or a MAX. None when nothing matched,
    so callers write `or 0` rather than assuming zero.
    """
    with connect() as conn:
        row = conn.execute(sql, params).fetchone()
        return row[0] if row else None


def load_library() -> list[dict]:
    """Every active test, with its embedding decoded."""
    rows = query("SELECT * FROM audit_tests WHERE is_active = 1 ORDER BY id")
    for row in rows:
        row["embedding"] = json.loads(row["embedding"]) if row["embedding"] else []
    return rows


# A proposal against a clause of a circular that has since been REISSUED. It is not
# rejected — nobody judged it — it simply describes a version of the text that no longer
# stands. It leaves the queue, and it is never exported.
#
# The old proposal is WITHDRAWN, not deleted: it is what the system proposed at the time,
# a reviewer may already have read it, and the replacement is easier to trust when the
# thing it replaced is still on the record.
WITHDRAWN = "Withdrawn (superseded)"

# A "No action" proposal is a clause that WAS assessed and needs nothing done. It is not
# a change, it never enters the sign-off queue, and it is never exported. Counting it as
# a proposed change makes the dashboard disagree with every screen that lists changes —
# so it is excluded here, once, rather than in each caller.
#
# Every screen, count and export filters on this ONE string. When the dashboard and the
# tables disagreed about how many changes there were, it was because this rule existed in
# six places and had been fixed in five. Import it; do not retype the condition.
def is_change(alias: str = "") -> str:
    """The SQL condition for "this proposal is a live change". Pass the table alias.

    `documents` also has a `status` column, so an unqualified condition is ambiguous in
    every query that joins the two — and SQLite raises at runtime, not at import, which
    means on a screen rather than in a test. `is_change("p")` in a join; `is_change()`
    only when proposals is the sole table.
    """
    prefix = f"{alias}." if alias else ""
    return f"{prefix}change_type != 'No action' AND {prefix}status != '{WITHDRAWN}'"


IS_CHANGE = is_change()
_IS_CHANGE = IS_CHANGE      # the old private name, kept so nothing in-flight breaks


# ====== DOCUMENT LIFECYCLE ======

def mark_processed(conn: sqlite3.Connection, document_id: int) -> None:
    """Record that every stage finished for this document.

    Call it ONLY after splitting has succeeded. Called too early — at intake, say — and a
    document that later crashed would look complete for ever and never be retried, which
    is the exact failure `processed_at` exists to prevent.
    """
    conn.execute("UPDATE documents SET processed_at = ? WHERE id = ?",
                 (datetime.now().isoformat(timespec="seconds"), document_id))


def is_complete(conn: sqlite3.Connection, document_id: int) -> bool:
    """True when this document finished a previous run and needs nothing further."""
    row = conn.execute("SELECT processed_at FROM documents WHERE id = ?",
                       (document_id,)).fetchone()
    return bool(row and row["processed_at"])


def has_human_decisions(conn: sqlite3.Connection, document_id: int) -> bool:
    """True if a person has approved, rejected or edited anything from this document.

    The line between what the machine may overwrite and what it may not. Everything the
    pipeline produced is reproducible from the source file; a human decision is not.
    """
    return bool(conn.execute(
        "SELECT 1 FROM proposals p "
        "WHERE p.document_id = ? AND ("
        "  p.status NOT IN (?, ?) "
        "  OR EXISTS (SELECT 1 FROM approvals a WHERE a.proposal_id = p.id) "
        "  OR EXISTS (SELECT 1 FROM proposal_revisions r WHERE r.proposal_id = p.id)) "
        "LIMIT 1",
        (document_id, "Pending review", WITHDRAWN)).fetchone())


def clear_document_work(conn: sqlite3.Connection, document_id: int) -> bool:
    """Delete the clauses, obligations and proposals of one document. False if refused.

    Everything here is reproducible from the source file, so deleting it costs only time —
    UNLESS a human has acted on a proposal, which is not reproducible at all. That check is
    the whole reason this is a function rather than three DELETEs at the call site.
    """
    if has_human_decisions(conn, document_id):
        return False
    conn.execute("DELETE FROM obligations WHERE clause_id IN "
                 "(SELECT id FROM clauses WHERE document_id = ?)", (document_id,))
    conn.execute("DELETE FROM proposals WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM clauses WHERE document_id = ?", (document_id,))
    return True


def clause_count(conn: sqlite3.Connection, document_id: int) -> int:
    """How many clauses this document already has. Zero for one never segmented."""
    return conn.execute("SELECT COUNT(*) FROM clauses WHERE document_id = ?",
                        (document_id,)).fetchone()[0]


def reclaim(conn: sqlite3.Connection, document_id: int) -> bool:
    """Delete a half-processed document so the same file can be read again. Returns False
    if it was left alone.

    This is the recovery path for a run that died between writing the document row and
    splitting it into clauses. Without it that document is skipped for ever, because every
    later run sees the row and calls the file already processed.

    **It refuses when a human has touched anything from the document.** That should be
    impossible — an unfinished document has no proposals for anyone to act on — but this
    function deletes proposals, and a delete that can reach an approved row on the day
    some other assumption turns out to be wrong is not worth the two lines it saves.
    """
    if not clear_document_work(conn, document_id):
        return False
    conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    return True


def supersede(conn: sqlite3.Connection, old_id: int, new_id: int) -> dict:
    """Link a reissued document to the one it replaces and clear the old queue entries.

    Returns {"withdrawn": n, "kept": n} — `kept` is the count a human has already acted
    on, which is the number worth putting in front of someone.

    Two rules, and the second is the one that matters:

    * A proposal nobody has touched is WITHDRAWN. It describes wording that no longer
      stands, so leaving it in the queue asks a reviewer to approve a superseded clause.
    * A proposal already approved, rejected or edited is LEFT EXACTLY AS IT IS. The
      pipeline does not get to reverse a human decision because a new file arrived — that
      is a judgement about whether the reissue changed anything material, and it belongs
      to the audit team. All the system does is say so, loudly.
    """
    conn.execute("UPDATE documents SET superseded_by = ? WHERE id = ?", (new_id, old_id))
    conn.execute("UPDATE documents SET supersedes = ? WHERE id = ?", (old_id, new_id))

    untouched = [r["id"] for r in conn.execute(
        "SELECT p.id FROM proposals p "
        "WHERE p.document_id = ? AND p.status IN ('Pending review', 'Changes requested') "
        # No-action rows were never in a queue, so withdrawing them changes nothing a
        # reviewer sees and only inflates the number this function reports. The count is
        # read out to a person; it has to mean what it says.
        "  AND p.change_type != 'No action' "
        "  AND NOT EXISTS (SELECT 1 FROM approvals a WHERE a.proposal_id = p.id) "
        "  AND NOT EXISTS (SELECT 1 FROM proposal_revisions r WHERE r.proposal_id = p.id)",
        (old_id,)).fetchall()]
    for proposal_id in untouched:
        conn.execute("UPDATE proposals SET status = ? WHERE id = ?", (WITHDRAWN, proposal_id))

    kept = conn.execute(
        f"SELECT COUNT(*) FROM proposals WHERE document_id = ? AND {IS_CHANGE}",
        (old_id,)).fetchone()[0]
    return {"withdrawn": len(untouched), "kept": kept}


# ====== THE FIGURES THE DASHBOARDS SHOW ======

def approval_counts() -> dict:
    """How many proposals sit at each sign-off status, EXCLUDING No-action rows.

    No-action is not a change: it never enters the queue and never exports. Counting it
    here is what made the dashboard disagree with every screen that lists changes.
    """
    rows = query(f"SELECT status, COUNT(*) n FROM proposals WHERE {_IS_CHANGE} "
                 "GROUP BY status")
    return {r["status"]: r["n"] for r in rows}


def counts() -> dict:
    """Every headline figure the dashboards show, in one query set.

    `assessed` is every clause the engine ruled on; `proposals` is the subset that
    proposes a change. They differ by the No-action rows, which are reported separately
    rather than hidden — otherwise the totals on screen cannot be reconciled.
    """
    return {
        "tests": scalar("SELECT COUNT(*) FROM audit_tests") or 0,
        "documents": scalar("SELECT COUNT(*) FROM documents WHERE status != 'duplicate'") or 0,
        "duplicates": scalar("SELECT COUNT(*) FROM documents WHERE status = 'duplicate'") or 0,
        "nested": scalar("SELECT COUNT(*) FROM documents WHERE parent_id IS NOT NULL") or 0,
        "failed": scalar("SELECT COUNT(*) FROM documents WHERE status = 'error'") or 0,
        "ocr_pages": scalar("SELECT COALESCE(SUM(ocr_pages), 0) FROM documents") or 0,
        "clauses": scalar("SELECT COUNT(*) FROM clauses") or 0,
        "actionable": scalar("SELECT COUNT(*) FROM clauses WHERE is_actionable = 1") or 0,
        # "assessed" is every clause the engine ruled on; "proposals" is the subset that
        # proposes a change, and is the number every other screen lists. They differ by
        # the No-action rows, which are reported in their own right.
        "assessed": scalar("SELECT COUNT(*) FROM proposals") or 0,
        "proposals": scalar(f"SELECT COUNT(*) FROM proposals WHERE {_IS_CHANGE}") or 0,
        "no_action": scalar("SELECT COUNT(*) FROM proposals "
                            "WHERE change_type = 'No action'") or 0,
        "superseded": scalar("SELECT COUNT(*) FROM documents "
                             "WHERE superseded_by IS NOT NULL") or 0,
        "withdrawn": scalar("SELECT COUNT(*) FROM proposals WHERE status = ?",
                            (WITHDRAWN,)) or 0,
        "flagged_splits": scalar("SELECT COUNT(*) FROM documents "
                                 "WHERE segment_warning IS NOT NULL") or 0,
        "obligations": scalar("SELECT COUNT(*) FROM obligations") or 0,
        "multi_obligation_clauses": scalar(
            "SELECT COUNT(*) FROM (SELECT clause_id FROM obligations "
            "GROUP BY clause_id HAVING COUNT(*) > 1)") or 0,
        "truncated_for_model": scalar("SELECT COUNT(*) FROM clauses "
                                      "WHERE model_input_truncated = 1") or 0,
        "approved": scalar(f"SELECT COUNT(*) FROM proposals WHERE status = 'Approved' "
                           f"AND {_IS_CHANGE}") or 0,
        "pending": scalar("SELECT COUNT(*) FROM proposals "
                          f"WHERE status NOT IN ('Approved', 'Rejected') AND {_IS_CHANGE}") or 0,
    }
