"""The human review step, and the eAudit hand-off.

The agreed solution's process flow is:

    circular intake → AI analysis → Excel working file → HUMAN REVIEW → eAudit

Everything before "human review" was already in the MVP; this module adds the last two
stages so the demonstration matches the flow that was proposed.

Kept deliberately small. The delivered system has named users, roles, an append-only
audit log and version snapshots on every edit. Here there is one reviewer at level 1,
one approver at level 2, and a simple decision log — enough to show the shape of the
governance without building it.
"""

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from . import config, store

# Re-exported from store so there is ONE definition of each status string. A status
# spelled slightly differently in two files is a proposal that silently stops
# matching its own queue.
WITHDRAWN = store.WITHDRAWN

PENDING_L1 = "Pending review"
PENDING_L2 = "Pending approval"
APPROVED = "Approved"
REJECTED = "Rejected"
CHANGES = "Changes requested"

NAVY = "1F3864"
HEAD_FILL = PatternFill("solid", fgColor=NAVY)
HEAD_FONT = Font(bold=True, color="FFFFFF", size=10)
THIN = Side(style="thin", color="D0D7DE")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def _log(conn, proposal_id: int, level: int, decision: str, note: str) -> None:
    store.insert(conn, "approvals", {
        "proposal_id": proposal_id, "level": level, "decision": decision,
        "note": note or "", "decided_at": datetime.now().isoformat(timespec="seconds"),
    })


# Which statuses a decision at each level may act on. A level-1 decision on something
# already past level 1 is not a valid action, and must not reach the decision log —
# a duplicate approval in an audit trail is a finding, not a cosmetic problem.
_ACCEPTS = {
    1: {PENDING_L1, CHANGES},
    2: {PENDING_L2},
}


def current_status(proposal_id: int) -> str:
    """The proposal's status now. Read before every decision, so a stale browser tab cannot
    apply a decision that no longer makes sense.
    """
    row = store.one("SELECT status FROM proposals WHERE id = ?", (proposal_id,))
    return (row or {}).get("status", PENDING_L1)


def decide(proposal_id: int, level: int, decision: str, note: str = "") -> str:
    """Record one review decision and return the proposal's new status.

    Level 1 approval advances to level 2; it does not finalise. Only level 2 approval
    marks a proposal Approved, and only Approved proposals can be exported.

    A decision that does not match the proposal's current state is refused: nothing is
    written and nothing is logged. That stops a double-click, a stale browser tab or a
    replayed request from putting two decisions on one proposal.
    """
    status_now = current_status(proposal_id)
    if status_now not in _ACCEPTS.get(level, set()):
        return status_now                       # not actionable at this level — no-op

    with store.connect() as conn:
        if decision == APPROVED:
            status = APPROVED if level >= 2 else PENDING_L2
            new_level = 2 if level == 1 else 2
        elif decision == REJECTED:
            status, new_level = REJECTED, level
        else:
            status, new_level = CHANGES, 1        # an edit sends it back to level 1

        field = "reviewer_note" if level == 1 else "approver_note"
        conn.execute(
            f"UPDATE proposals SET status = ?, current_level = ?, {field} = ? WHERE id = ?",
            (status, new_level, note, proposal_id))
        _log(conn, proposal_id, level, decision, note)
    return status


def decide_many(proposal_ids: list[int], level: int, decision: str, note: str = "") -> int:
    """Apply one decision to several proposals. Returns how many were attempted.

    Each goes through decide(), so a proposal that is not actionable at this level is a
    no-op rather than a second decision on the same row.
    """
    for proposal_id in proposal_ids:
        decide(proposal_id, level, decision, note)
    return len(proposal_ids)


def reset_all() -> None:
    # An eAudit file written before the reset would otherwise survive and still be
    # offered for download, implying approvals that no longer exist.
    """Return every proposal to Pending review and clear the sign-off trail. DEMO ONLY.

    Also deletes a previously written eAudit file: leaving it on disk would keep offering
    a download that implies approvals which no longer exist.
    """
    stale = config.OUTPUT_DIR / "eAudit_BAC_Export.xlsx"
    if stale.exists():
        stale.unlink()
    with store.connect() as conn:
        conn.execute("UPDATE proposals SET status = ?, current_level = 1, "
                     "reviewer_note = NULL, approver_note = NULL", (PENDING_L1,))
        conn.execute("DELETE FROM approvals")
        conn.execute("DELETE FROM proposal_revisions")


# ====== EDITS ======

# What a reviewer is allowed to change before signing off. Anything not listed here is
# the system's own record of what it decided and why — `rationale`, `candidates`,
# `confidence` and every `existing_*` snapshot are never rewritten by a human, or the
# audit trail stops being evidence of what the engine actually proposed.
EDITABLE = {
    "proposed_test_description": "Proposed test",
    "proposed_exception_description": "Proposed exception",
    "risk_rating": "Risk rating",
    "department": "Audit department",
}


def edit(proposal_id: int, field: str, new_value: str, level: int,
         note: str = "") -> bool:
    """Record one human edit. Returns False if nothing actually changed.

    The old value is written to `proposal_revisions` BEFORE the proposal is updated, so
    the chain from what the engine proposed to what was approved is always recoverable.
    """
    if field not in EDITABLE:
        raise ValueError(f"{field} is not editable")

    row = store.one(f"SELECT {field} AS value FROM proposals WHERE id = ?", (proposal_id,))
    old_value = (row or {}).get("value") or ""
    if (new_value or "").strip() == old_value.strip():
        return False

    with store.connect() as conn:
        store.insert(conn, "proposal_revisions", {
            "proposal_id": proposal_id, "field": field,
            "old_value": old_value, "new_value": new_value,
            "changed_by": "reviewer (level 1)" if level == 1 else "approver (level 2)",
            "level": level, "note": note,
            "changed_at": datetime.now().isoformat(timespec="seconds"),
        })
        conn.execute(f"UPDATE proposals SET {field} = ? WHERE id = ?",
                     (new_value, proposal_id))
    return True


def revisions(proposal_id: int) -> list[dict]:
    """Every human edit to one proposal, oldest first — field, old value, new value, who, when."""
    return store.query(
        "SELECT field, old_value, new_value, changed_by, note, changed_at "
        "FROM proposal_revisions WHERE proposal_id = ? ORDER BY id", (proposal_id,))


def history(proposal_id: int) -> list[dict]:
    """Every sign-off decision on one proposal, oldest first."""
    return store.query(
        "SELECT level, decision, note, decided_at FROM approvals "
        "WHERE proposal_id = ? ORDER BY id", (proposal_id,))


# ====== eAUDIT HAND-OFF ======

EAUDIT_COLUMNS = [
    ("Test Code (for eAudit BAC)", 22), ("Test (for eAudit BAC)", 62),
    ("Exception Code (for eAudit BAC)", 24), ("Exception (for eAudit BAC)", 52),
    ("Risk Rating (for eAudit BAC)", 18), ("Circular Reference Number (for eAudit)", 34),
    ("Audit Department", 16), ("Change Type", 14),
    ("Approved by (level 2)", 22), ("Approved on", 20),
]


def export_eaudit() -> tuple[Path | None, list[str]]:
    """Write the eAudit hand-off file. APPROVED PROPOSALS ONLY.

    Returns (path, refusals). If anything in scope is unapproved the export still runs
    for the approved rows, but every unapproved item is named — the delivered system
    refuses outright, and the demo shows the same check being made.
    """
    approved = store.query(
        "SELECT p.*, d.filename, d.title FROM proposals p "
        "JOIN documents d ON d.id = p.document_id "
        f"WHERE p.status = ? AND {store.is_change('p')} ORDER BY p.id",
        (APPROVED,))

    unapproved = store.query(
        # A withdrawn proposal is not an outstanding approval — its circular was
        # reissued and it never needed a decision. Listing it as a refusal would
        # block an export for work nobody is expected to do.
        f"SELECT sr_no, status FROM proposals WHERE status != ? AND {store.IS_CHANGE}",
        (APPROVED,))
    refusals = [f"{r['sr_no']} — {r['status']}" for r in unapproved]

    if not approved:
        return None, refusals

    wb = Workbook()
    sheet = wb.active
    sheet.title = "eAudit BAC"
    for i, (name, width) in enumerate(EAUDIT_COLUMNS, start=1):
        cell = sheet.cell(row=1, column=i, value=name)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        sheet.column_dimensions[chr(64 + i)].width = width
    sheet.row_dimensions[1].height = 30
    sheet.freeze_panes = "A2"

    for row_index, p in enumerate(approved, start=2):
        decision = store.one(
            "SELECT decided_at FROM approvals WHERE proposal_id = ? AND level = 2 "
            "ORDER BY id DESC LIMIT 1", (p["id"],))
        values = [
            p["target_test_code"] or p["sr_no"],
            p["proposed_test_description"] or p["existing_test_description"] or "",
            p["exception_code"] or "",
            p["proposed_exception_description"] or "",
            p["risk_rating"] or "", p["title"] or p["filename"] or "",
            p["department"] or "", p["change_type"],
            "approver (level 2)", (decision or {}).get("decided_at", ""),
        ]
        for col, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_index, column=col, value=value)
            cell.border = BORDER
            cell.font = Font(size=9)
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    config.ensure_dirs()
    path = config.OUTPUT_DIR / "eAudit_BAC_Export.xlsx"
    wb.save(path)
    return path, refusals
