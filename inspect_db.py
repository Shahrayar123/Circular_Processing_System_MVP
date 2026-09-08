"""Read the demo database from the command line. READ ONLY — never writes.

    python inspect_db.py                     # everything, summarised
    python inspect_db.py documents           # what arrived, format, nesting, failures
    python inspect_db.py clauses             # actionability, and who judged it
    python inspect_db.py proposals           # the changes, before and after
    python inspect_db.py edits               # what a human changed, from what
    python inspect_db.py approvals           # the sign-off trail
    python inspect_db.py show 2026-W36-19-IB19   # one proposal, in full

    python inspect_db.py sql "SELECT ..."    # any read-only query

For when you are on the client machine with no SQLite browser installed, and for
answering "where is that actually stored" without opening the UI.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mvp import config, store  # noqa: E402

WIDTH = 100


# ====== OUTPUT ======

def banner(text: str) -> None:
    """Print a section heading."""
    print()
    print(text)
    print("-" * len(text))


def rows(records: list[dict], limit: int = 40) -> None:
    """Print records as an aligned table, trimming long values rather than wrapping."""
    if not records:
        print("   (none)")
        return

    columns = list(records[0])
    shown = records[:limit]
    widths = {c: min(max(len(c), *(len(str(r.get(c) or "")) for r in shown)), 52)
              for c in columns}

    print("   " + "  ".join(c[:widths[c]].ljust(widths[c]) for c in columns))
    print("   " + "  ".join("-" * widths[c] for c in columns))
    for record in shown:
        print("   " + "  ".join(
            str(record.get(c) or "—")[:widths[c]].ljust(widths[c]) for c in columns))
    if len(records) > limit:
        print(f"   … {len(records) - limit} more row(s)")


def block(label: str, text: str) -> None:
    """Print a labelled block of text, wrapped to the terminal width rather than truncated —
    this is where clause and test wording is read, so it must not be cut.
    """
    print(f"   {label}")
    for line in (text or "—").splitlines() or ["—"]:
        while len(line) > WIDTH:
            print(f"      {line[:WIDTH]}")
            line = line[WIDTH:]
        print(f"      {line}")


# ====== SECTIONS ======

def show_documents() -> None:
    """What arrived: format, nesting, and anything unreadable."""
    banner("DOCUMENTS — what arrived")
    rows(store.query(
        "SELECT id, filename, kind, source, pages, ocr_pages, parent_id, status "
        "FROM documents ORDER BY id"))

    nested = store.query(
        "SELECT c.filename AS child, p.filename AS found_inside "
        "FROM documents c JOIN documents p ON p.id = c.parent_id")
    if nested:
        print("\n   Found inside another document:")
        rows(nested)

    failed = store.query(
        "SELECT filename, error FROM documents WHERE status = 'error'")
    if failed:
        print("\n   Could not be read:")
        rows(failed)

    # A suspicious split is not an error, so it would otherwise never be looked at.
    flagged = store.query("SELECT filename, segment_warning FROM documents "
                          "WHERE segment_warning IS NOT NULL")
    if flagged:
        print("\n   Split looks wrong — check these:")
        rows(flagged)


def show_clauses() -> None:
    """What was read, who judged it, and which rung split it."""
    banner("CLAUSES — what was read, and who judged it")
    counts = store.query(
        "SELECT COALESCE(judged_by, 'rules') AS judged_by, COUNT(*) AS clauses, "
        "SUM(is_actionable) AS actionable FROM clauses GROUP BY judged_by "
        "ORDER BY clauses DESC")
    rows(counts)
    print("\n   A clause judged by a FALLBACK is labelled as such. A run that quietly")
    print("   degraded to keyword rules must not look like one that used the model.")

    print()
    rows(store.query(
        "SELECT c.sequence, c.clause_ref, c.page_number, c.is_actionable, c.reason, "
        "substr(c.text, 1, 70) AS text FROM clauses c ORDER BY c.document_id, c.sequence"),
        limit=15)


def show_obligations() -> None:
    """Clauses that carry more than one duty, and the proposals each produced."""
    banner("OBLIGATIONS — where one clause states several duties")
    rows(store.query(
        "SELECT COUNT(*) AS obligations, "
        "(SELECT COUNT(*) FROM (SELECT clause_id FROM obligations GROUP BY clause_id "
        " HAVING COUNT(*) > 1)) AS clauses_with_several FROM obligations"))
    print()
    multi = store.query(
        "SELECT c.clause_ref, o.sequence, o.strata_tag, substr(o.text, 1, 62) AS duty "
        "FROM obligations o JOIN clauses c ON c.id = o.clause_id "
        "WHERE o.clause_id IN (SELECT clause_id FROM obligations GROUP BY clause_id "
        "                      HAVING COUNT(*) > 1) "
        "ORDER BY o.clause_id, o.sequence")
    rows(multi)
    if not multi:
        print("   No clause in this run states more than one duty.")
    print()
    print("   Each duty is retrieved, decided and approved on its own, but they share")
    print("   one clause reference, page and character offsets.")


def show_proposals() -> None:
    """The proposed changes, by type and in clause order."""
    banner("PROPOSALS — the changes, before and after")
    rows(store.query(
        "SELECT change_type, COUNT(*) AS n FROM proposals "
        "GROUP BY change_type ORDER BY n DESC"))
    print()
    rows(store.query(
        "SELECT p.sr_no, p.change_type, p.department, p.risk_rating, p.status, "
        "p.target_test_code AS existing_test, "
        "substr(p.proposed_test_description, 1, 46) AS proposed "
        "FROM proposals p WHERE p.change_type != 'No action' "
        "ORDER BY p.document_id, p.id"), limit=25)


def show_edits() -> None:
    """Every human edit to a proposal, from what to what."""
    banner("EDITS — what a human changed, from what, to what")
    edits = store.query(
        "SELECT p.sr_no, r.field, r.changed_by, r.changed_at, r.note, "
        "substr(r.old_value, 1, 40) AS was, substr(r.new_value, 1, 40) AS now "
        "FROM proposal_revisions r JOIN proposals p ON p.id = r.proposal_id "
        "ORDER BY r.id")
    rows(edits)
    if not edits:
        print("   Nothing has been edited yet — the trail fills as reviewers work.")


def show_approvals() -> None:
    """The sign-off trail, and where every change has reached."""
    banner("APPROVALS — the sign-off trail")
    rows(store.query(
        "SELECT p.sr_no, a.level, a.decision, a.decided_at, a.note "
        "FROM approvals a JOIN proposals p ON p.id = a.proposal_id ORDER BY a.id"))
    print()
    rows(store.query(
        "SELECT status, COUNT(*) AS n FROM proposals "
        "WHERE change_type != 'No action' GROUP BY status"))


def show_one(sr_no: str) -> None:
    """One proposal in full — the answer to "what exactly does this change"."""
    proposal = store.one(
        "SELECT p.*, c.clause_ref, c.text AS clause_text, c.page_number, "
        "COALESCE(d.title, d.filename) AS circular "
        "FROM proposals p JOIN clauses c ON c.id = p.clause_id "
        "JOIN documents d ON d.id = p.document_id WHERE p.sr_no = ?", (sr_no,))
    if not proposal:
        print(f"No proposal with Sr # {sr_no}.")
        return

    banner(f"{proposal['sr_no']}  ·  {proposal['change_type']}  ·  {proposal['status']}")
    print(f"   {proposal['circular']}  —  clause {proposal['clause_ref']}, "
          f"page {proposal['page_number']}")
    print()
    block("SOURCE CLAUSE", proposal["clause_text"])
    print()

    # The frozen "before". Not a live read of audit_tests: the library moves, and the
    # question is what the approver signed a change AGAINST.
    if proposal["target_test_code"]:
        print(f"   BEFORE  ({proposal['target_test_code']} · "
              f"{proposal['existing_department'] or '—'} · risk "
              f"{proposal['existing_risk_rating'] or '—'} · "
              f"{proposal['existing_source_reference'] or 'no reference'})")
        block("test", proposal["existing_test_description"])
        block("exception", proposal["existing_exception_description"])
    else:
        print("   BEFORE  none — no existing test covers this obligation")
    print()
    print(f"   AFTER   ({proposal['department'] or '—'} · risk "
          f"{proposal['risk_rating'] or '—'} · strata {proposal['strata'] or '—'})")
    block("test", proposal["proposed_test_description"])
    block("exception", proposal["proposed_exception_description"])
    print()
    block("WHY", proposal["rationale"])
    print(f"\n   decided {proposal['decided_at'] or '—'} · "
          f"confidence {proposal['confidence']}")

    edits = store.query(
        "SELECT field, old_value, new_value, changed_by, changed_at, note "
        "FROM proposal_revisions WHERE proposal_id = ? ORDER BY id", (proposal["id"],))
    if edits:
        banner("CHANGED BY A HUMAN SINCE")
        for edit in edits:
            print(f"   {edit['field']} — {edit['changed_by']}, {edit['changed_at']}"
                  + (f" ({edit['note']})" if edit["note"] else ""))
            block("from", edit["old_value"])
            block("to", edit["new_value"])

    trail = store.query(
        "SELECT level, decision, note, decided_at FROM approvals "
        "WHERE proposal_id = ? ORDER BY id", (proposal["id"],))
    if trail:
        banner("SIGN-OFF")
        rows(trail)


def show_summary() -> None:
    """The headline counts, as the dashboards show them."""
    counts = store.counts()
    banner("SUMMARY")
    for key in ("tests", "documents", "duplicates", "nested", "failed", "ocr_pages",
                "clauses", "actionable", "assessed", "proposals", "no_action",
                "approved", "pending"):
        print(f"   {key:<14} {counts.get(key, 0)}")


SECTIONS = {
    "documents": show_documents,
    "clauses": show_clauses,
    "obligations": show_obligations,
    "proposals": show_proposals,
    "edits": show_edits,
    "approvals": show_approvals,
}


# ====== ENTRY POINT ======

def main() -> int:
    """Entry point. Returns a process exit code; never writes."""
    if not store.ready():
        print("The demo database has not been built yet.\n")
        print("    python run_pipeline.py\n")
        print("Run that once, then try again. The database is generated, so it is not")
        print("in the repository and every machine builds its own.")
        return 1

    args = sys.argv[1:]
    print(f"Database: {config.DB_PATH}")

    if not args:
        show_summary()
        for section in SECTIONS.values():
            section()
        print("\nOne proposal in full:  python inspect_db.py show <Sr #>")
        return 0

    command = args[0].lower()

    if command == "show":
        if len(args) < 2:
            print("Give a Sr # — e.g. python inspect_db.py show 2026-W36-19-IB19")
            return 1
        show_one(args[1])
        return 0

    if command == "sql":
        if len(args) < 2:
            print('Give a query — e.g. python inspect_db.py sql "SELECT * FROM clauses"')
            return 1
        statement = args[1]
        # Read only, deliberately. This file is for looking, and a typo here should not
        # be able to change what the reviewer sees in the UI.
        if not statement.lstrip().lower().startswith(("select", "with")):
            print("Only SELECT queries — inspect_db.py never writes.")
            return 1
        banner(statement[:WIDTH])
        rows(store.query(statement), limit=100)
        return 0

    if command in SECTIONS:
        SECTIONS[command]()
        return 0

    print(f"Unknown section '{command}'. Try: "
          + ", ".join(SECTIONS) + ", show <Sr #>, sql \"SELECT ...\"")
    return 1


if __name__ == "__main__":
    sys.exit(main())
