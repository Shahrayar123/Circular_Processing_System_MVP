"""The Word working document — one per circular.

The audit team's own convention, taken from the annotated circulars ABL supplied: a
short note against each actionable clause naming the proposal it produced, and
"Information" against the rest.

The real system anchors these as true Word comments through an OOXML helper. The MVP
puts them in a right-hand column of a two-column table, which renders the same idea
without the OOXML work.
"""

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

from . import config, store

NAVY = RGBColor(0x1F, 0x38, 0x64)
GREY = RGBColor(0x59, 0x59, 0x59)
GREEN = RGBColor(0x2E, 0x7A, 0x4F)
AMBER = RGBColor(0x9C, 0x6F, 0x11)
RED = RGBColor(0xB0, 0x3A, 0x30)

COLOURS = {"New": GREEN, "Amendment": AMBER, "Deletion": RED, "No action": GREY}


# ====== THE AUDIT TEAM'S COMMENT CONVENTION ======

def _note(proposal: dict | None) -> tuple[str, RGBColor]:
    if not proposal:
        return "Information", GREY
    change = proposal["change_type"]
    if change == "New":
        return f"New test {proposal['sr_no']}", GREEN
    if change == "Amendment":
        return f"Covered in {proposal['target_test_code']} — amended", AMBER
    if change == "Deletion":
        target = proposal["target_test_code"] or "affected tests"
        return f"Deletion — {target} withdrawn", RED
    return "Information", GREY


# ====== THE DOCUMENT ======

def _styled_run(paragraph, text: str, *, size: float, colour=None, bold: bool = False):
    """Add one run of text with the size, colour and weight given. Returns the run.

    Every piece of styled text in this file goes through here. python-docx has no
    stylesheet, so without it each paragraph repeats four lines of font assignment and
    the document drifts out of visual consistency one edit at a time.
    """
    run = paragraph.add_run(text)
    # Only set bold when it is wanted. Assigning False writes an explicit <w:b w:val="0"/>
    # into the XML where python-docx would otherwise omit the element — the same on screen,
    # but a different file, which matters when the output is diffed or version-controlled.
    if bold:
        run.bold = True
    run.font.size = Pt(size)
    if colour is not None:
        run.font.color.rgb = colour
    return run


def _write_header(document, doc_row) -> None:
    """Title, source circular, and where the file came from."""
    _styled_run(document.add_paragraph(), "Audit Checklist Working Document",
                size=18, colour=NAVY, bold=True)
    _styled_run(document.add_paragraph(), doc_row["title"] or doc_row["filename"],
                size=12, colour=GREY)
    _styled_run(document.add_paragraph(),
                f"Source: {doc_row['source']}   ·   File: {doc_row['filename']}"
                f"{'   ·   Dated: ' + doc_row['doc_date'] if doc_row['doc_date'] else ''}",
                size=9, colour=GREY)

    document.add_paragraph()
    _styled_run(document.add_paragraph(), "Circular text and audit working notes",
                size=13, colour=NAVY, bold=True)


def _new_table(document):
    """The two-column table: the circular's own words on the left, our note on the right.

    Side by side on purpose. It is the layout ABL's team already uses, and it lets a
    reviewer check a proposed test against the clause it came from without scrolling.
    """
    table = document.add_table(rows=1, cols=2)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.columns[0].width = Inches(4.9)
    table.columns[1].width = Inches(2.2)

    for cell, label in zip(table.rows[0].cells, ("Clause", "Audit working note")):
        _styled_run(cell.paragraphs[0], label, size=10, colour=NAVY, bold=True)
    return table


def _write_clause_row(table, clause: dict, proposal: dict | None) -> None:
    """One clause and its working note, as a row of the table."""
    note, colour = _note(proposal)
    row = table.add_row().cells

    left = row[0].paragraphs[0]
    _styled_run(left, f"{clause['clause_ref']}  ", size=9, colour=NAVY, bold=True)
    _styled_run(left, clause["text"], size=9.5)

    right = row[1].paragraphs[0]
    right.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _styled_run(right, note, size=9, colour=colour, bold=True)

    # The detail line is only for a live proposed change. A No-action clause has nothing
    # to propose, and a withdrawn one describes a version of the circular that no longer
    # stands — printing either would put wording in front of a reviewer that is not up
    # for approval.
    if (proposal and proposal["change_type"] != "No action"
            and proposal["status"] != store.WITHDRAWN):
        detail_text = (proposal["proposed_test_description"]
                       or proposal["rationale"])[:230]
        _styled_run(right, "\n" + detail_text, size=8, colour=GREY)


def _output_path(doc_row) -> Path:
    """Where this document is written.

    Named after the SOURCE FILE, not the detected title: several circulars can share a
    title line and would silently overwrite each other. The id prefix keeps them ordered
    and unique even when two files have the same name.
    """
    stem = Path(doc_row["filename"]).stem
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in stem)[:55].strip()
    return config.OUTPUT_DIR / f"{doc_row['id']:02d}_{safe or 'document'}_working.docx"


def build(document_id: int) -> Path:
    """Write the Word working document for one circular and return its path."""
    doc_row = store.one("SELECT * FROM documents WHERE id = ?", (document_id,))
    clauses = store.query(
        "SELECT * FROM clauses WHERE document_id = ? ORDER BY sequence", (document_id,))
    proposals = {p["clause_id"]: p for p in store.query(
        "SELECT * FROM proposals WHERE document_id = ?", (document_id,))}

    document = Document()
    for section in document.sections:
        section.left_margin = section.right_margin = Inches(0.7)

    _write_header(document, doc_row)
    table = _new_table(document)
    for clause in clauses:
        _write_clause_row(table, clause, proposals.get(clause["id"]))

    config.ensure_dirs()
    path = _output_path(doc_row)
    document.save(path)
    return path


def build_all(document_ids: list[int] | None = None) -> list[Path]:
    """Write a working document for each circular given. Returns the paths written.

    `document_ids` limits it to those circulars — normally the ones a run just processed.
    Passing None writes one for EVERY readable circular in the database, which is a
    deliberate, occasional action and not what a weekly run should do:

    * **It overwrites.** `build()` saves over the existing file, and this document is the
      audit team's working copy — the thing they annotate. Rewriting last month's working
      document because an unrelated circular arrived this week can destroy someone's work,
      and nothing would say so.
    * It is wasted effort. Nothing about an old circular changed just because a new one
      was read.

    Regenerating everything IS right when the review state has moved — proposals edited or
    approved in the app — because those documents are then genuinely stale. That is what
    `--outputs all` is for.

    Duplicates and unreadable documents are skipped either way: there is no content to put
    in a working file, and their error shows in the queue instead.
    """
    sql = "SELECT id FROM documents WHERE status = 'ingested'"
    params: tuple = ()
    if document_ids is not None:
        if not document_ids:
            return []
        sql += f" AND id IN ({', '.join('?' for _ in document_ids)})"
        params = tuple(document_ids)
    return [build(r["id"]) for r in store.query(sql, params)]
