"""The Excel working file — ABL's own format, not ours.

The layout is taken column for column from the working file ABL supplied with their BRD
(`BRD Agentic AI Use Case ... APMQA ARRG .xlsx`): six sheets, **78 columns**, the header
row on **row 6** with grouping bands above it, and data from row 7. It is the file their
audit team already works in, so the shape is theirs and matching it exactly is the point —
a file with our own tidier columns would have to be re-keyed before anyone could use it.

**What this file does NOT invent.** Roughly thirty of the 78 columns are ABL master data —
EP activity and strata codes, the three standard-observation levels, reportable/risk-type
/sample-method flags, and the time-to-complete estimates. Those come from systems we do not
have (open questions 13 and 14). They are written EMPTY. Filling them with plausible values
would be the same failure as inventing a test code: it reaches a reviewer looking like fact.
ABL's own file leaves several of them as `#N/A` for the same reason.

Formatting follows the BRD: additions in green text, deletions in red strike-through —
text formatting, not cell fills.
"""

from datetime import date, datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import config, store

NAVY = "1F3864"
GREY_BAND = "D9D9D9"
HEAD_FILL = PatternFill("solid", fgColor=NAVY)
BAND_FILL = PatternFill("solid", fgColor=GREY_BAND)
HEAD_FONT = Font(bold=True, color="FFFFFF", size=9)
BAND_FONT = Font(bold=True, size=9)
BODY = Font(size=9)
THIN = Side(style="thin", color="BFBFBF")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)

HEADER_ROW = 6          # ABL's file puts the column names on row 6, not row 1
FIRST_DATA_ROW = 7

# ====== WHAT A CHANGE LOOKS LIKE ======
#
# Three change types, three looks, chosen so the sheet reads two ways: scan the Test Type
# column to see what KIND of change each row is, or read any wording cell and see whether
# that text is being added or taken away.
#
# TEXT formatting carries the meaning, not cell fill. The BRD is explicit — additions in
# green text, deletions in red strike-through — and a fill across the wording columns
# would fight both the grey grouping bands on row 5 and the conditional formatting ABL
# apply to their own copy. The single fill is on the Test Type cell, which holds a label
# rather than wording and so has nothing to colour.
#
# "Amendment in yellow" is honoured as DARK AMBER text plus a pale yellow fill on the Test
# Type cell — deliberately not yellow text. Yellow on white is a contrast ratio of roughly
# 1.1:1: invisible on screen and blank on a printed page, which is where an approver
# actually signs. The colour reads as yellow where it is a fill and stays legible where it
# is text. Do not "correct" this to a pure yellow font.
AMBER = "B45F06"

# Columns 11 and 14 — the wording being PROPOSED. On a Deletion these hold the existing
# wording instead, because a deletion proposes nothing; struck through is then literally
# correct, which is why Deletion appears in both maps with the same font.
PROPOSED_FONT = {
    "New":       Font(color="008000", size=9),
    "Amendment": Font(color=AMBER, size=9),
    "Deletion":  Font(color="C00000", size=9, strike=True),
}

# Columns 13 and 16 — the wording as the approved checklist holds it TODAY. Struck through
# on an Amendment as well as a Deletion: on an amendment this text is being replaced, and
# showing the old and the new in one row with only the new marked leaves the reviewer to
# work out which is which. Grey rather than red, because amended wording is superseded,
# not removed. A New has nothing here.
EXISTING_FONT = {
    "Amendment": Font(color="808080", size=9, strike=True),
    "Deletion":  Font(color="C00000", size=9, strike=True),
}

# Column 5 only — so a reviewer can find every deletion in a 200-row sheet by colour
# rather than by filtering.
TYPE_FILL = {
    "New":       PatternFill("solid", fgColor="E2EFDA"),
    "Amendment": PatternFill("solid", fgColor="FFF2CC"),
    "Deletion":  PatternFill("solid", fgColor="FCE4E4"),
}

PROPOSED_COLUMNS = (11, 14)
EXISTING_COLUMNS = (13, 16)
TYPE_COLUMN = 5


# The 78 columns, in ABL's order and with their exact wording — including the line breaks
# and trailing spaces, because the header text is what their own macros and filters match
# on. Do not "tidy" these strings.
COLUMNS = [
    ('Week', 12),
    ('Sr #', 9),
    ('Audit Dep\n(BA, MA, IS&CA, RR, SA)', 9),
    ('Related Checklist (if any)', 9),
    ('Test Type (New/Amendment/Deletion/Tagged to different strata)', 13),
    ('Amendment Type (Amendment in Test, Exception, Strata, reference, risk etc.)', 15),
    ('Amendments Proposed in ', 14),
    ('Sr # of Previous Weeks along with Year ', 9),
    ('Strata', 11),
    ('Activity name', 15),
    ('New Test / Changes in Existing Test (If any)', 74),
    ('Existing Test Code (if any)', 16),
    ('Existing Test (as per latest approved Checklist)', 51),
    ('New Exception / Changes in Existing Exception (if any)', 54),
    ('Existing Exception Code (if any)', 18),
    ('Existing Exception (as per latest approved Checklist)', 34),
    ('Existing/Proposed Risk Rating', 17),
    ('Marking \n(Onsite / Offsite / Partially Offsite)', 18),
    (' Offsite\xa0Based on Data (Yes/No)', 14),
    ('Offsite\xa0 Based on Scanned Documents (Yes/No)', 13),
    ('System and Source of Verification', 24),
    ('Root Cause\n(Policy Gap, Process/Procedure Gap, System Gap, Implementation Gap)', 28),
    ('Regulatory Violation (Yes/No)', 19),
    ('Existing/Proposed revision in Circular Reference Number', 24),
    ('Circular Name (Summary)', 28),
    ('Circular Reference Number (for Summary & MIS)', 31),
    ('Date of Circular (for Summary & MIS)', 13),
    ('Remarks1', 34),
    ('1', 3),
    ('CATEGORY', 21),
    ('EP Activity Name', 19),
    ('EP Activity Long Name', 26),
    ('EP Activity Code', 17),
    ('EP Strata Name (To be filled manually exactly as per EP sheet)', 21),
    ('EP Strata Long Name', 23),
    ('EP Strata Code', 17),
    ('Related to Voucher\n(Voucher/Non-Voucher/MIS)', 20),
    ('Observation Name (Level 1)', 22),
    ('Observation Custom Code\n(Level 1)', 27),
    ('Level Sequence Number (Standard Observation Level 1)', 21),
    ('Observation Name (Level 2)', 20),
    ('Observation Custom Code (Level 2)', 26),
    ('Level Sequence Number (Standard Observation Level 2)', 20),
    ('Observation Name (Level 3)', 18),
    ('Observation Custom Code (Level 3)', 20),
    ('Level 3 Sequence Number (Standard Observation Level 3)', 27),
    ('2', 4),
    ('Reportable', 21),
    ('Risk Type', 15),
    ('Observation Type', 15),
    ('Key Observation', 16),
    ('Sample Calculation Method', 19),
    ('Rectifiable', 16),
    ('Significant Finding', 14),
    ('Leakage of Income', 17),
    ('Exception Type', 22),
    ('Pledge Stocks', 13),
    ('3', 4),
    ('Time Calc. Method', 18),
    ('Time to Complete On-Site', 15),
    ('Time to Complete Off-Site', 17),
    ('Remarks2', 24),
    ('Remarks3', 18),
    ('Reason for Addition/Deletion/Amendment (On basis of BSG remarks/recommendations etc.)', 31),
    ('Test Code (for eAudit BAC)', 44),
    ('Test (for eAudit BAC)', 21),
    ('Exception Code (for eAudit BAC)', 38),
    ('Exception (for eAudit BAC)', 24),
    ('Risk Rating (for eAudit BAC)', 30),
    ('Circular Reference Number (for eAudit)', 19),
    (None, 40),
    (None, 47),
    (None, 40),
    (None, 16),
    (None, 30),
    (None, 13),
    (None, 13),
    (None, 13),
]

# The grey bands on row 5 that group related columns, as (first column, last column, label).
BANDS = [
    (6, 8, "Amendments"),
    (31, 34, "EP activity list"),
    (35, 37, "EP sub activity"),
    (38, 40, "Standard Observation Level 1 - Existing (as per latest approved)/proposed"),
    (41, 43, "Standard Observation Level 2 - Existing (as per latest approved)/proposed"),
    (44, 46, "Standard Observation Level 3 - Existing (as per latest approved)/proposed"),
    (48, 57, "Additional Fields"),
    (59, 61, "Time working"),
]

# ABL's vocabulary is not ours, and the LOV sheet validates against theirs exactly.
# "Amendment " really does carry a trailing space in their dropdown list; matching it is
# what keeps the cell valid when their file opens it.
TEST_TYPE = {"New": "New", "Amendment": "Amendment ", "Deletion": "Deletion",
             "Tagged to different EP strata": "Tagged to different EP strata"}

# They write the risk level as "High Risk", not "High".
RISK = {"High": "High Risk", "Medium": "Medium Risk", "Low": "Low Risk"}

# The dropdown vocabularies, carried into the output so the file still validates when
# ABL open it. Taken from the hidden LOV sheet of their own workbook.
LOV = [
    ("Test Type (New/Amendments/Deletion)", list(TEST_TYPE.values())),
    ("Marking (Onsite/Offsite/Partially Offsite)", ["Onsite", "Offsite", "Partially Offsite"]),
    ("Off-Site Based on Data (Yes/No)", ["Yes", "No"]),
    ("Off-Site Based on Scanned Documents (Yes/No)", ["Yes", "No"]),
    ("Regulatory Violation (Yes/No)", ["Yes", "No"]),
    ("Reportable", ["Yes", "No"]),
    ("Risk Type", ["Operational", "Functional", "Reputaional ", "Credit Risk"]),
    ("Observation Type", config.ROOT_CAUSES),
    ("Key Observation", ["Yes", "No"]),
    ("Sample Calculation Method", ["Exception from EP sample", "Exception from Additional sample",
                                   "General Exception"]),
    ("Rectifiable", ["Yes", "No"]),
    ("Leakage of Income", ["Yes", "No"]),
    ("Exception Type", ["Exception from EP sample", "Exception from Additional sample",
                        "General Exception"]),
    ("Root Cause", config.ROOT_CAUSES),
    ("Amendments Proposed in", ["Approved checklist Implemented in eAudit",
                                "Approved checklist not yet implemented in eAudit"]),
    ("Pledge Stock", ["Yes", "No"]),
    ("Amendment Type", ["Amendment in Test ", "Amendment in Exception ",
                        "Amendment in Reference ", "Amendment in Risk Level",
                        "Amendment in Strata", "Amendment in Annexure", "Other"]),
    ("Audit Dep (BA, MA, IS&CA, RR, SA)", config.DEPARTMENTS),
    ("System and Source of Verification", ["T24 Live", "T24 CDB", "IMS", "ITS", "eAudit"]),
    ("Related Checklist (if any)", ["BAC", "MA", "IS", "CA", "RR", "SA"]),
]


def _week_label(when: date) -> str:
    """ABL write the week as `38 (15.09.2025 - 21.09.2025)` — number, then the Monday to
    Sunday it covers. The number alone is ambiguous across years and reads badly in a
    file someone opens in March."""
    year, week, weekday = when.isocalendar()
    monday = when.fromordinal(when.toordinal() - weekday + 1)
    sunday = when.fromordinal(monday.toordinal() + 6)
    return f"{week} ({monday.strftime('%d.%m.%Y')} - {sunday.strftime('%d.%m.%Y')})"


def _row_values(p: dict, doc: dict, week: str) -> dict:
    """Column number -> value, for the columns we can honestly fill.

    Everything absent from this mapping is left EMPTY in the sheet. That is a deliberate
    statement: the empty cells are the ones needing ABL master data or a human judgement,
    and a reviewer can see at a glance which is which.
    """
    change = p["change_type"]
    proposed_test = p["proposed_test_description"] or ""
    circular = doc.get("title") or doc.get("filename") or ""

    values = {
        1: week,
        2: p["sr_no"],
        3: p["department"],
        4: p["department"],                       # "Related Checklist" — same in the demo
        5: TEST_TYPE.get(change, change),
        9: p["strata"],
        17: RISK.get(p["risk_rating"], p["risk_rating"]),
        22: p["root_cause"],
        23: "Yes",                                # every proposal here comes FROM a circular
        24: circular,
        25: circular,
        26: doc.get("doc_date") or "",
        27: doc.get("doc_date") or "",
        64: p["rationale"],
    }

    if change == "Amendment":
        values[6] = "Amendment in Test "
        values[7] = "Approved checklist Implemented in eAudit"

    # The three "existing" columns are the FROZEN snapshot taken when the decision was
    # made — never a live lookup, because the library moves and a reviewer must see the
    # wording they are actually approving a change against.
    if p["target_test_code"]:
        values[12] = p["target_test_code"]
        values[13] = p["existing_test_description"] or ""
    if p["existing_exception_code"]:
        values[15] = p["existing_exception_code"]
        values[16] = p["existing_exception_description"] or ""

    if change == "Deletion":
        # A deletion proposes no new wording. The cell shows what is being REMOVED, so an
        # approver reading this file offline can see the control they are retiring rather
        # than only its code.
        values[11] = p["existing_test_description"] or ""
        values[14] = p["existing_exception_description"] or ""
    else:
        values[11] = proposed_test
        values[14] = p["proposed_exception_description"] or ""

    return values


def _change_font(index: int, change: str, value) -> Font:
    """How this cell's text should look, given what the row is doing to the checklist.

    Only the wording columns are marked. Colouring the whole row would say "this row is a
    deletion" — which the Test Type column already says — instead of "THIS TEXT is what
    goes away", which is the thing an approver has to see before signing.
    """
    if not value:
        return BODY
    if index in PROPOSED_COLUMNS:
        return PROPOSED_FONT.get(change, BODY)
    if index in EXISTING_COLUMNS:
        return EXISTING_FONT.get(change, BODY)
    return BODY


def _proposed_tests(wb: Workbook, proposals: list[dict], docs: dict) -> None:
    """The working file itself, in ABL's 78-column layout."""
    sheet = wb.create_sheet("Proposed Tests")

    for index, (header, width) in enumerate(COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = max(width, 9)

    for first, last, label in BANDS:
        sheet.merge_cells(start_row=5, start_column=first, end_row=5, end_column=last)
        cell = sheet.cell(5, first, label)
        cell.fill, cell.font = BAND_FILL, BAND_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    for index, (header, _width) in enumerate(COLUMNS, start=1):
        cell = sheet.cell(HEADER_ROW, index, header)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[HEADER_ROW].height = 58
    sheet.sheet_view.zoomScale = 95
    # Frozen at the first data cell, so the headers and the Week/Sr # columns stay put
    # while you scroll 78 columns sideways. ABL's own copy is frozen at B34, which is
    # where their cursor happened to be parked rather than a decision about the format.
    sheet.freeze_panes = sheet.cell(FIRST_DATA_ROW, 3)

    week = _week_label(date.today())
    for offset, p in enumerate(proposals):
        row = FIRST_DATA_ROW + offset
        doc = docs.get(p["document_id"], {})
        values = _row_values(p, doc, week)
        for index in range(1, len(COLUMNS) + 1):
            cell = sheet.cell(row, index, values.get(index, ""))
            cell.border = BORDER
            cell.alignment = Alignment(wrap_text=True, vertical="top")
            cell.font = _change_font(index, p["change_type"], values.get(index))
            if index == TYPE_COLUMN:
                cell.fill = TYPE_FILL.get(p["change_type"], cell.fill)


def _summary(wb: Workbook, proposals: list[dict]) -> None:
    """Department-wise counts, in ABL's own shape: one row per department, then a total.

    Their sheet lists all five departments even where the count is zero, and it is right
    to keep that: a department missing from the table reads as "not looked at", while a
    zero reads as "nothing came up this week".
    """
    sheet = wb.active
    sheet.title = "Summary"
    sheet.column_dimensions["A"].width = 34
    for letter in "BCDEFG":
        sheet.column_dimensions[letter].width = 15

    # ABL turn gridlines off on this sheet and view it at 90%. Cosmetic, but it is their
    # file: it should open looking like the one they closed.
    sheet.sheet_view.showGridLines = False
    sheet.sheet_view.zoomScale = 90

    title = sheet.cell(1, 1, "DEPARTMENT WISE SUMMARY")
    title.font = Font(bold=True, size=12, color=NAVY)

    types = ["New", "Amendment", "Deletion", "Tagged to different EP strata"]
    headers = ["Audit Department (Full Name)", "Audit Department (Short Name)",
               *types, "Total No. of Tests "]
    for index, header in enumerate(headers, start=1):
        cell = sheet.cell(3, index, header)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    totals = [0] * len(types)
    for offset, short in enumerate(config.DEPARTMENTS):
        row = 4 + offset
        sheet.cell(row, 1, config.DEPARTMENT_NAMES[short]).border = BORDER
        sheet.cell(row, 2, short).border = BORDER
        line = 0
        for index, change in enumerate(types):
            count = sum(1 for p in proposals
                        if p["department"] == short and p["change_type"] == change)
            totals[index] += count
            line += count
            cell = sheet.cell(row, 3 + index, count)
            cell.border, cell.alignment = BORDER, Alignment(horizontal="center")
        cell = sheet.cell(row, 7, line)
        cell.border, cell.font = BORDER, Font(bold=True, size=9)
        cell.alignment = Alignment(horizontal="center")

    row = 4 + len(config.DEPARTMENTS)
    for index, total in enumerate([*totals, sum(totals)]):
        cell = sheet.cell(row, 3 + index, total)
        cell.border, cell.font = BORDER, Font(bold=True, size=9)
        cell.alignment = Alignment(horizontal="center")


WEEK_MIS_COLUMNS = [
    ("Sr. #", 7), ("Circular ", 10), ("Repeat", 9), ("Common", 9),
    ("Circular Category", 15), ("Reference", 20), ("Circular Issuance Date", 16),
    ("Circular Email Date", 16), ("Description", 46),
    ("Related SBP/ABL Reference, If Any.", 22),
    ("Previous ABL/SBP Circular referred, if any", 22), ("GRADING", 12),
    ("Checklist Updation", 16), ("Action", 14), ("GRADING Reviewed by UH-APM", 16),
    ("Are the new tests added Regulatory in nature ?", 18),
    ("Actionable (Yes/No)", 12), ("Tagged to for working", 16), ("To", 10),
    ("Department", 12), ("Dated", 12), ("w for sharing", 12), ("Worked in Week of ", 16),
]


def _week_mis(wb: Workbook, docs: list[dict], proposals: list[dict]) -> None:
    """One row per circular received this period — including the ones that needed nothing.

    A circular that produced no proposal still has to appear here, marked Actionable = No.
    It is the sheet that answers "did anyone look at this?", and a circular missing from
    it is indistinguishable from one that never arrived.
    """
    sheet = wb.create_sheet("Week MIS")
    for index, (header, width) in enumerate(WEEK_MIS_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
        cell = sheet.cell(2, index, header)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[2].height = 42
    sheet.sheet_view.showGridLines = False
    sheet.sheet_view.zoomScale = 80

    week = _week_label(date.today())
    by_document: dict[int, int] = {}
    for p in proposals:
        by_document[p["document_id"]] = by_document.get(p["document_id"], 0) + 1

    for offset, doc in enumerate(docs):
        row = 3 + offset
        count = by_document.get(doc["id"], 0)
        values = {
            1: offset + 1,
            2: "SBP" if str(doc.get("filename", "")).upper().startswith("BPRD") else "ABL",
            5: "INST" if count else "INF",
            6: doc.get("title") or doc.get("filename") or "",
            7: doc.get("doc_date") or "",
            8: doc.get("doc_date") or "",
            9: doc.get("title") or doc.get("filename") or "",
            13: f"{count} test(s) proposed" if count else "No change required",
            16: "Yes" if count else "No",
            17: "Yes" if count else "No",
            23: week,
        }
        for index in range(1, len(WEEK_MIS_COLUMNS) + 1):
            cell = sheet.cell(row, index, values.get(index, ""))
            cell.border, cell.font = BORDER, BODY
            cell.alignment = Alignment(wrap_text=True, vertical="top")


ANNEXURE_COLUMNS = [
    ("Proposed Test Sr #", 16), ("Proposed Test Sr # (unique)", 15),
    ("Test Type (New/Amendments/Deletion)", 17), ("EP Activity Code", 16),
    ("EP Strata Code", 20), ("Test Code", 17), ("Exception Code", 16),
    ("Column Order", 22), ("Column Name", 29), ("Alias", 13),
    ("Column Type\n1 = Data Set\n2 = Annexure\n3=General Exceptions", 38),
    ("Audit Program Name", 15), ("Remarks", 18),
]


def _annexure(wb: Workbook, proposals: list[dict]) -> None:
    """The data-column definitions a new Branch Audit test needs, one row per column.

    In ABL's file each new BA test is followed by a row per data column the auditor will
    work from — `CO_CODE_1 / Br.Code / 1`, `AMOUNT / AMOUNT / 1`, and so on. Those column
    lists are audit design: somebody decides what data a test needs, and no circular says
    it. So the sheet is written with the tests KEYED IN and the definitions left blank.

    That is deliberate, and better than omitting the sheet. Omitted, the working file is
    the wrong shape and whoever fills it in has to build the keying by hand; present and
    empty, it says exactly which tests are waiting on a data-column design and gives them
    somewhere to put it. Only NEW Branch Audit proposals appear — an amendment reuses the
    existing test's annexure, and a deletion needs none.
    """
    sheet = wb.create_sheet("Annexure")
    for index, (header, width) in enumerate(ANNEXURE_COLUMNS, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width

    # Reusing a cell's `.font` after assignment hands back a StyleProxy, which openpyxl
    # cannot hash into its style table. Build the Font once and assign it to each cell.
    note_font = Font(italic=True, size=8, color="808080")
    for column in (2, 3):
        cell = sheet.cell(1, column, "This column will auto populate based on Column A")
        cell.font = note_font

    for index, (header, _width) in enumerate(ANNEXURE_COLUMNS, start=1):
        cell = sheet.cell(2, index, header)
        cell.fill, cell.font, cell.border = HEAD_FILL, HEAD_FONT, BORDER
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    sheet.row_dimensions[2].height = 42

    row = 3
    for p in proposals:
        if p["change_type"] != "New" or p["department"] != "BA":
            continue
        for index, value in ((1, p["sr_no"]), (2, p["sr_no"]),
                             (3, TEST_TYPE.get(p["change_type"], p["change_type"]))):
            cell = sheet.cell(row, index, value)
            cell.border, cell.font = BORDER, BODY
        for index in range(4, len(ANNEXURE_COLUMNS) + 1):
            cell = sheet.cell(row, index, "")
            cell.border, cell.font = BORDER, BODY
        row += 1

    # Their Annexure keeps gridlines on and sits at 80%.
    sheet.sheet_view.zoomScale = 80


def _lov(wb: Workbook) -> None:
    """The dropdown vocabularies, carried into the output.

    ABL's file keeps this sheet hidden and every validated column points at it. Writing
    the file without it leaves those columns with no vocabulary to check against, so the
    dropdowns a reviewer expects are simply gone — the file still opens, and quietly
    accepts any value typed into a column that is supposed to be constrained.
    """
    sheet = wb.create_sheet("LOV")
    for index, (name, values) in enumerate(LOV, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = 26
        cell = sheet.cell(1, index, name)
        cell.fill, cell.font = HEAD_FILL, HEAD_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
        for offset, value in enumerate(values, start=2):
            sheet.cell(offset, index, value).font = BODY
    sheet.sheet_state = "hidden"


def check_format(path: Path) -> list[str]:
    """Read the written file back and report anything that does not match ABL's layout.

    Written as a separate pass over the SAVED file, not as assertions while building it.
    A formatter that checks its own intentions proves nothing; this opens the artefact
    that will actually be sent and looks at what is really in it.
    """
    from openpyxl import load_workbook

    problems = []
    wb = load_workbook(path)
    for name in ("Summary", "Proposed Tests", "Annexure", "Week MIS", "LOV"):
        if name not in wb.sheetnames:
            problems.append(f"sheet {name!r} is missing")
    if "Proposed Tests" not in wb.sheetnames:
        return problems

    sheet = wb["Proposed Tests"]
    if sheet.max_column < len(COLUMNS):
        problems.append(f"Proposed Tests has {sheet.max_column} columns, expected "
                        f"{len(COLUMNS)}")
    for index, (header, _width) in enumerate(COLUMNS, start=1):
        actual = sheet.cell(HEADER_ROW, index).value
        if actual != header:
            problems.append(f"column {index} header is {actual!r}, expected {header!r}")

    # Formatting is part of the deliverable, not decoration: "formatting compliance 100%"
    # is an acceptance target, and a font that failed to apply is invisible here and
    # obvious the moment the client opens the file. Checked on the SAVED workbook, because
    # a font object assigned in memory and a font written into the XML are not the same
    # claim — openpyxl silently ignores a style set on a merged or shared cell.
    problems += _check_change_formatting(sheet)
    return problems


def _rgb(font) -> str:
    """The six-digit colour of a font, or "" when it has none.

    openpyxl reports colours as eight-character ARGB, and a theme colour has no `rgb` at
    all — it raises on attribute access rather than returning None, so both cases are
    handled here instead of at four call sites.
    """
    try:
        value = font.color.rgb
        return str(value)[-6:].upper() if isinstance(value, str) else ""
    except AttributeError:
        return ""


def _check_change_formatting(sheet) -> list[str]:
    """Every wording cell carries the marking its row's change type requires."""
    problems = []
    for row in range(FIRST_DATA_ROW, sheet.max_row + 1):
        # ABL's LOV writes "Amendment " with a trailing space; the maps are keyed without.
        change = str(sheet.cell(row, TYPE_COLUMN).value or "").strip()
        if not change:
            continue

        fill = TYPE_FILL.get(change)
        if fill and sheet.cell(row, TYPE_COLUMN).fill.fgColor.rgb != fill.fgColor.rgb:
            problems.append(f"row {row}: Test Type cell is not filled for a {change}")

        for columns, fonts in ((PROPOSED_COLUMNS, PROPOSED_FONT),
                               (EXISTING_COLUMNS, EXISTING_FONT)):
            expected = fonts.get(change)
            if not expected:
                continue
            for index in columns:
                cell = sheet.cell(row, index)
                if not cell.value:
                    continue
                if _rgb(cell.font) != _rgb(expected):
                    problems.append(f"row {row} column {index}: {change} text is "
                                    f"{_rgb(cell.font) or 'unset'}, expected {_rgb(expected)}")
                if bool(cell.font.strike) != bool(expected.strike):
                    problems.append(f"row {row} column {index}: {change} text "
                                    f"{'is' if cell.font.strike else 'is not'} struck through, "
                                    f"expected the opposite")
    return problems


def build() -> Path:
    """Write the working file and return its path."""
    proposals = store.query(
        f"SELECT * FROM proposals WHERE {store.IS_CHANGE} ORDER BY document_id, id")
    documents = store.query("SELECT * FROM documents WHERE status = 'ingested' ORDER BY id")
    by_id = {d["id"]: d for d in documents}

    wb = Workbook()
    _summary(wb, proposals)
    _proposed_tests(wb, proposals, by_id)
    _annexure(wb, proposals)
    _week_mis(wb, documents, proposals)
    _lov(wb)

    config.ensure_dirs()
    path = config.OUTPUT_DIR / "Audit_Checklist_Working_File.xlsx"
    wb.save(path)

    problems = check_format(path)
    if problems:
        # Reported, never raised. A formatting slip must not lose the run's work, and the
        # file is more useful in someone's hands with a note attached than not written.
        print(f"   FORMAT CHECK: {len(problems)} issue(s) in {path.name}")
        for problem in problems[:5]:
            print(f"      {problem}")
    return path
