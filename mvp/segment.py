"""Split a document into clauses and judge which ones create an obligation.

Splitting is deterministic — numbering and paragraph breaks, no model. The actionability
judgement is the part that needs language understanding, so it is made by the model:

    python run_pipeline.py --judge llm        # a local model reads each clause
    python run_pipeline.py                    # keyword rules, instant, no model

Both are kept. The rules are the fallback when the model is unreachable, and they are
what makes the pipeline runnable on a laptop with nothing installed. Every clause records
WHICH judged it, because a demo that quietly degrades to rules while claiming to use a
model is worse than one that never had the model.
"""

import json
import re
import time

from . import config, store

# A numbered clause opener: "3.", "3)", "(iii)", "3.2", "iv."
_NUMBERED = re.compile(r"^\s*(\(?\d+(?:\.\d+)*\)?[.)]?|\(?[ivxlIVXL]{1,5}\)[.)]?)\s+(?=[A-Z\"'“])")

# Wording that creates a duty
_OBLIGATION = re.compile(
    r"\b(shall|must|are required to|is required to|required to|will be required|"
    r"should ensure|shall ensure|are advised|is advised|advised to|"
    r"shall not|must not|may not|are prohibited|is prohibited|"
    r"shall stand withdrawn|stand withdrawn|supersed\w*|no longer applicable|"
    r"cease to have effect|is hereby|shall be discontinued|shall be disabled)\b",
    re.IGNORECASE,
)

# Wording that means "read this, do nothing"
_INFORMATIONAL = re.compile(
    r"^\s*(dear |yours |regards|please refer|reference is made|with reference to|"
    r"for information|this is for|table of contents|page \d+|annexure|encl)",
    re.IGNORECASE,
)

# A spreadsheet row, as extract.py writes it: "[row 4] A-1 | Banks shall ... | 01-Oct".
# Annexures to SBP circulars are very often tables — one requirement per row — so a row
# is a clause boundary in its own right. Without this the whole sheet arrives as one
# block and five separate obligations collapse into a single proposal.
_SHEET_ROW = re.compile(r"^\[row (\d+)\]\s*(.*)$")

# A short first cell that is really the row's own clause id: "A-1", "3.2", "(iv)".
_ROW_REF = re.compile(r"^\(?[A-Za-z]{0,3}[-.]?\d+(?:\.\d+)*\)?$")


def _row_ref(row_number: str, body: str) -> str:
    """Prefer the annexure's own clause id over the spreadsheet row number."""
    first = body.split("|")[0].strip()
    return first if _ROW_REF.match(first) else f"row {row_number}"


_SUPERSESSION = re.compile(
    r"\b(supersed\w*|shall stand withdrawn|stand withdrawn|no longer applicable|"
    r"cease to have effect|is hereby cancelled|rescind\w*)\b", re.IGNORECASE)

# Which strata a clause is about — demo heuristic over ABL's real vocabulary
_STRATA_HINTS = [
    ("Internet Banking", r"internet bank|digital channel|privileged (account|access)|"
                        r"authentication|session|password|cyber|penetration"),
    ("Branchless Banking", r"branchless|agent|level 0|biometric"),
    ("Fraud Prevention", r"fraud|forged|fake|call back|cbc|signature verification"),
    ("Compliance", r"compliance|kyc|aml|cft|due diligence|sanction|politically exposed|"
                   r"beneficial owner|suspicious transaction"),
    ("Data & Reporting", r"report(ing|s)?|return|data quality|reconcil|bi report|dashboard"),
    ("Account Opening", r"account opening|onboard|account holder|dormant|activation"),
    ("Cash & Teller", r"cash|teller|vault|retention limit"),
    ("Credit", r"financ(e|ing)|loan|obligor|provision|classified asset"),
    ("Remittances", r"remittance|transfer|swift|payment order"),
]


# ====== LEAD-IN SENTENCES AND THEIR LISTS ======
#
# Circulars constantly write an obligation as a stem plus a list:
#
#     The risk assessment shall cover at least the following aspects:
#     a) A current and detailed description of the bank's business ...
#     b) Internet Banking assets are identified and prioritised.
#
# Two things go wrong if this is left alone. The stem becomes a clause with no content
# — "shall cover the following aspects" is not a testable obligation — and the list
# becomes ONE clause, so seven separate requirements produce a single proposal and six
# audit tests are never written.
#
# The stem and the list are also routinely separated: the stem sits at the foot of one
# page and the list starts on the next, and in a marked-up draft a column of review
# comments can land between them in reading order. So a list looks BACKWARDS for its
# stem rather than a stem looking forwards.

_LIST_ITEM = re.compile(
    r"^\(?(?P<mark>[a-zA-Z]|[ivxIVX]{1,5}|\d{1,2})[).]\s+|^\s*[•▪]\s+")

_LEAD_IN = re.compile(r":\s*$")
_MAX_LEAD_IN_CHARS = 320          # a stem is a sentence, not a paragraph
_LEAD_IN_LOOKBACK = 2500          # far enough to cross a page and a margin column
_MIN_LIST_ITEM_CHARS = 25         # "b) Assets are prioritised." is a real obligation


# The first marker of a real enumeration. Requiring the list to START at its opener is
# what stops a stem binding to the wrong list: a marked-up circular carries quoted
# fragments like "vi. Implement ISMS2 ... ix. Ensure that ..." in a margin column, and
# those sit between a stem and the list it actually introduces. A fragment beginning at
# "vi." is a quotation, not the list this sentence opened.
_OPENERS = {"a", "A", "i", "I", "1"}


def _states_a_duty(piece: str) -> bool:
    """True for a fragment too short to pass the length rule that is an obligation anyway.

    The length rule exists to drop headings, signature lines and column headers, and it
    does that well — measured on this corpus it drops 105 such fragments. But length is a
    proxy, and it is wrong for the short imperative sentences circulars actually use:
    "Banks shall reconcile ATM cassettes daily." is 42 characters and is a real audit
    test. Obligation wording is direct evidence, so it overrides the proxy.

    This matters most for DELETIONS. "Circular 04 of 2019 stands withdrawn." is 37
    characters, and losing it means the bank keeps auditing against a rule that no longer
    exists — the most damaging miss this system has.
    """
    return (len(piece) >= config.MIN_OBLIGATION_CLAUSE_CHARS
            and bool(_OBLIGATION.search(piece)))


def _split_list_items(piece: str) -> tuple[str, list[str]]:
    """Break "The following apply: a) ... b) ..." into (stem, items).

    The stem is whatever precedes the first marker in the same block — often the whole
    sentence that makes the items testable — and is returned separately so it can be
    carried onto each item rather than surviving as a clause of its own. Returns
    ("", []) when the text is not a list.
    """
    # Each marker may open with a bracket — "(a)", "(ii)", "(3)" — and the split falls BEFORE
    # the bracket. Splitting after it left "(" on the end of the stem, so every item of
    # "... carried out as follows: (a) ... (b) ..." read "follows: ( a) In respect of".
    for pattern in (r"(?=(?<![A-Za-z0-9(])\(?[a-z][)]\s)",       # a) b) c)   (a) (b) (c)
                    r"(?=(?<![A-Za-z0-9(])\(?[ivx]{1,5}[).]\s)",  # i) ii) iii) (i) (ii)
                    r"(?=(?<![A-Za-z0-9(])\(?\d{1,2}[)]\s)",      # 1) 2) 3)   (1) (2)
                    r"(?=[•▪]\s)"):
        parts = [p.strip() for p in re.split(pattern, piece) if p.strip()]

        # A NUMBERED clause opening a LETTERED list — "1. Banks shall ensure the following:
        # a) ... b) ..." — is the list's stem, not its first item. Read as an item, it was
        # carried onto nothing, and the lettered items lost the sentence that makes them
        # testable.
        head = _LIST_ITEM.match(parts[0]) if parts else None
        second = _LIST_ITEM.match(parts[1]) if len(parts) > 1 else None
        numbered_stem = bool(head and second
                             and (head.group("mark") or "").isdigit()
                             and not (second.group("mark") or "").isdigit())
        items = [p for p in (parts[1:] if numbered_stem else parts)
                 if _LIST_ITEM.match(p) and len(p) >= _MIN_LIST_ITEM_CHARS]

        # Two items make a list; one match is far more likely to be prose.
        if len(items) < 2:
            continue
        first = _LIST_ITEM.match(items[0])
        if first and (first.group("mark") or "") not in _OPENERS:
            continue                                  # starts mid-sequence: a quotation

        stem = parts[0] if parts and (numbered_stem or not _LIST_ITEM.match(parts[0])) else ""
        return stem, items
    return "", []


def _numeric_depth(ref) -> int | None:
    """How deep a numeric clause reference is — "3" is 1, "3.2" is 2 — or None when the
    reference is not a plain number ("a", "iv", "C-1", or no reference at all)."""
    if ref and re.fullmatch(r"\d+(?:\.\d+)*", str(ref)):
        return str(ref).count(".") + 1
    return None


def _attach_lead_ins(pieces: list[tuple]) -> list[tuple]:
    """Give every list item the stem that introduces it, and drop the bare stem.

    `pieces` is [(ref, text, start, end)] in document order; the same shape comes back.
    """
    stems: list[tuple] = []                         # (source index, start, text, ref)
    out: list[tuple] = []                           # (ref, text, start, end, source index)
    consumed: set[int] = set()

    for index, (ref, piece, start, end) in enumerate(pieces):
        inline_stem, items = _split_list_items(piece)

        # A numbered clause at the stem's own level or above ENDS that stem's list.
        # "2. ... the following arrangements shall apply:" owns "a." and "b."; it does not
        # own "3.", which is its sibling. Without this the stem stayed in reach and was
        # prepended to every numbered clause after it inside the lookback window: on a real
        # SBP circular (PSP&OD Circular Letter 01 of 2026) paragraphs 3 and 4 both arrived
        # carrying paragraph 2's opening sentence, and were retrieved and drafted as though
        # they were about fuel-station card pricing.
        depth = _numeric_depth(ref)
        while stems and depth is not None:
            stem_depth = _numeric_depth(stems[-1][3])
            if stem_depth is None or stem_depth < depth:
                break
            stems.pop()

        # The stem in reach: among the stems close enough to still be the sentence that
        # opened this list, one stating a duty wins a TIE. In a marked-up draft a
        # quotation's colon ("the policy states below:") sits closer to the list than the
        # real stem, and this is what keeps the real one. Wording breaks the tie; it never
        # decides whether there is a stem at all.
        pending = None
        in_reach = [s for s in stems if start - s[1] <= _LEAD_IN_LOOKBACK]
        if in_reach:
            pending = next((s for s in reversed(in_reach) if _OBLIGATION.search(s[2])),
                           in_reach[-1])

        if items:
            # A stem in the same block wins: it is unambiguously this list's sentence.
            stem_text = inline_stem
            if not stem_text and pending:
                stem_text = pending[2]
                consumed.add(pending[0])
            for item in items:
                # Every item of a list shares its parent block's span. They overlap, so
                # anything measuring coverage has to merge spans rather than sum them.
                out.append((ref, f"{stem_text} {item}" if stem_text else item,
                            start, end, index))
            continue

        # A list whose items are split across blocks — a page break falls between "a)"
        # and "b)", so each arrives alone and the two-item rule above never fires. A
        # marker at the very START of a block is a safe enough signal on its own; the
        # same marker mid-sentence is not, which is why this is not the general rule.
        # A list starts at its opener — `a)`, `i)`, `1.` or a bullet — exactly as the inline
        # rule requires. Once a stem is carrying a list, later items continue it. Without
        # this, "The following should not be the part of deposits:" claimed the circular's
        # own paragraph "4. Other instructions ..." as its first item.
        marker = _LIST_ITEM.match(piece)
        if marker and pending and (pending[0] in consumed or marker.group("mark") is None
                                   or marker.group("mark") in _OPENERS):
            consumed.add(pending[0])
            out.append((ref, f"{pending[2]} {piece}", start, end, index))
            continue

        # ANY sentence ending in a colon can open a list — wording does not decide it. "The
        # following instructions are to be complied with:" has no "shall" and is exactly
        # the sentence that makes its items testable; requiring a duty keyword sent those
        # items to the judge stripped of it. A stem whose list has already been used is
        # retired when a new stem appears, so it cannot outbid the new one.
        if _LEAD_IN.search(piece) and len(piece) <= _MAX_LEAD_IN_CHARS:
            stems = [s for s in stems if s[0] not in consumed]
            stems.append((index, start, piece, ref))
        out.append((ref, piece, start, end, index))

    # A stem whose list was found is now carried by every item; on its own it says
    # nothing testable, so it must not reach the reviewer as a clause of its own.
    return [(ref, piece, start, end) for ref, piece, start, end, index in out
            if index not in consumed]


# ====== TABLES UNDER A CLAUSE ======
#
# A table in a circular is one of two different things:
#
#   a SUPPORTING table — the data a clause above it depends on: "Branches shall not retain
#   cash above the limits given below", then a table of limits. Its rows are part of THAT
#   clause. Split off, they say nothing testable on their own — "Urban | 5,000,000 |
#   Regional Head" is 33 characters, fails the heading-length rule, and was DROPPED: the
#   clause survived saying "the limits given below" with no limits anywhere in the system.
#
#   everything else — an annexure where each row is its own requirement, an "existing |
#   revised" comparison, a signature block. Each row stays its own piece, as it always has.
#
# A table is attached only when all three gates hold. Each is there because a looser
# version was run against ABL's own documents and failed:
#
#   1. A READER marked where it starts (`[table]`) — Word, HTML and PowerPoint, the
#      formats that know where a table sits among paragraphs. In a spreadsheet every line
#      is a row, and a multi-line cell beginning "1." looks exactly like a clause with rows
#      after it — without this gate, unrelated rows of ABL's own working-file workbook
#      glued onto fragments of cells. PDF, OCR and plain text have no table structure at
#      all; they are handled separately, under UNMARKED TABLES.
#   2. Every cell is short — data, not prose (DATA_TABLE_MAX_WORDS). ABL's call-back
#      confirmation circular has a table whose cells run to 73 words; those rows are the
#      procedure's steps, and attaching them would collapse the steps into one proposal.
#   3. The paragraph DIRECTLY above the table points forward to it — "given below",
#      "following", "as under", a closing colon. Without this, the signature table at the
#      foot of ABL's circulars ("Irfan Saeed Dar | Safwan Khawaja") attached itself to the
#      last clause, and the signatories' names became part of a retrieval query.
#
# An attached table runs until a row states a duty of its own, or anything that is not a
# row arrives. A table failing any gate is split row by row exactly as before this rule
# existed — so the worst case of the rule is the old behaviour, never a new failure.

# Lines written by the Word, HTML and PowerPoint readers before and after every table
# (extract.TABLE_MARKER, extract.TABLE_END_MARKER). Matched as patterns here for the same
# reason `_SHEET_ROW` is: the splitter reads the readers' output format; it does not import
# the readers.
_TABLE_START = re.compile(r"^\[table\]$")
_TABLE_END = re.compile(r"^\[/table\]$")

# The clause reference of a table kept WHOLE as a clause of its own, because no paragraph
# pointed to it — see TABLES UNDER A CLAUSE, and UNMARKED TABLES for the PDF form.
TABLE_REF = "table"


def _table_rows(lines: list[str], position: int) -> list[str]:
    """The rows of the table whose start marker is at `lines[position]`, one string each.

    A cell holding several paragraphs arrives as several lines. Those continuation lines
    are folded back into the row they came from, so the data test measures each cell as it
    really is — a cell carrying a run of numbered clauses must read as the long prose it
    is, not as a short first line.
    """
    rows, closed = [], False
    for following in lines[position + 1:]:
        text = following.strip()
        if _TABLE_END.match(text):
            closed = True
            break
        row = _SHEET_ROW.match(text)
        if row:
            rows.append(row.group(2).strip())
        elif rows:
            rows[-1] = f"{rows[-1]} {text}"
    # A table whose end is not in this block spans a blank line — an empty paragraph inside
    # a cell — so its later rows are out of sight. Judged on its first rows alone, ABL's
    # "existing | revised" table looked like data because its header row was short, and was
    # kept whole. Unseen means NOT data: the table is split row by row, as before.
    return rows if closed else []

# Longest cell, in words, that still reads as data. The supporting tables this exists for
# have cells of one to four words — a category, an amount, an authority. The prose tables
# it must NOT capture start in the twenties. Eight leaves room for "Area Manager or Deputy
# Area Manager" without admitting a sentence.
DATA_TABLE_MAX_WORDS = 8

# What the paragraph immediately above a table says when the table is part of it.
_POINTS_FORWARD = re.compile(
    r"\b(below|as under|as follows|following|hereunder|under-?mentioned|given in the table)\b"
    r"|:\s*$",
    re.IGNORECASE)


def _is_data_table(rows: list[str]) -> bool:
    """True when every cell of every row is short enough to be data rather than prose."""
    return bool(rows) and all(len(cell.split()) <= DATA_TABLE_MAX_WORDS
                              for row in rows for cell in row.split(" | "))


def _can_own_table(entries: list[tuple[bool, str]], is_row: bool) -> bool:
    """True when the piece being built is a clause the table after it can belong to.

    Not a row piece: rows of an obligation table must not glue onto each other, or five
    requirements collapse into one proposal. Not a piece containing a `### SHEET` /
    `### SLIDE` marker: a table after one is on the next sheet or slide. And the LAST
    paragraph of the piece — the one printed immediately above the table — must point
    forward to it. The whole piece is deliberately not searched: an unnumbered circular
    arrives as one long piece, and an "as under" three paragraphs earlier says nothing
    about this table.
    """
    if not entries or is_row or any(text.startswith("###") for _, text in entries):
        return False
    last_paragraph = next((text for row, text in reversed(entries) if not row), "")
    return bool(_POINTS_FORWARD.search(last_paragraph))


# ====== UNMARKED TABLES ======
#
# PDF, OCR and plain text carry no table structure at all. A table comes out as one short
# line per row with the columns gone — "Urban 5,000,000 Regional Head" — usually separated
# from the clause above by a blank line. Each such block failed the heading-length rule and
# was dropped: a real SBP circular (DMMD Circular No. 03) kept "the following institutions
# have been selected ... as specified below:" and lost every institution it named.
#
# So data lines are attached to the clause before them when that clause's LAST sentence
# points forward — whether they arrive as blocks of their own after a blank line (OCR,
# plain text) or as the very next lines of the same block (native PDF) — and they keep
# attaching until anything else arrives. Each keeps a line of its own, so the drafted test
# carries them exactly as it carries a Word table's rows. Deliberately narrow, because
# this runs on every PDF:
#
#   * last sentence, not the whole piece — a PDF paragraph is one long piece, and a
#     "following" three sentences back says nothing about what comes next;
#   * a data line is short, states no duty, and is not a numbered clause, a list item, a
#     lead-in or a letter's sign-off — numbered and lettered lists keep their own handling
#     (`_attach_lead_ins`), which carries the stem onto each item as its own clause.
#
# And ONLY for unstructured text (`split_clauses(unstructured=True)`). Word, HTML,
# PowerPoint and spreadsheets mark their real tables, and a short line after a colon there
# is a bullet the author typed. Run on ABL's own BRD, this recognition turned every bullet
# list under "Maintain:" and "must ensure:" into table rows.


# A clause number the way clauses are numbered — "2.", "2.1)", "(3)" — as opposed to a
# serial-number column. "2 NATIONAL BANK OF PAKISTAN" is row 2 of a list whose columns
# were lost; "2. Any excess shall be reported" is a clause. `_NUMBERED` accepts both,
# because OCR often drops the dot after a clause number — right for splitting clauses,
# wrong for recognising table rows: it ended the DMMD list after its first institution.
_NUMBERED_CLAUSE = re.compile(r"^\s*\(?\d+(?:\.\d+)*[.)]\s")


def _word_count(text: str) -> int:
    """Words and numbers in a line, not counting marks standing alone ("-", "|", "—").
    Counted with the dash, "Category B - metropolitan, no currency chest 25,000,000
    30,000,000" was nine words, one over the limit, and broke a real table in two."""
    return len(re.findall(r"[A-Za-z0-9][\w'’.,&/()-]*", text))


def _is_data_line(line: str) -> bool:
    """True for a line that reads as a table row with its columns gone."""
    text = line.strip()
    return bool(text) and (
        _word_count(text) <= DATA_TABLE_MAX_WORDS
        and not _OBLIGATION.search(text)
        and not _NUMBERED_CLAUSE.match(text)
        and not _LIST_ITEM.match(text)
        and not _LEAD_IN.search(text)
        and not _INFORMATIONAL.match(text)
        and not _SHEET_ROW.match(text)
        and not text.startswith(("###", "[table]", "[/table]")))


def _is_data_block(block: str) -> bool:
    """True when every line of a blank-line-separated block is a data line."""
    lines = [line for line in block.split("\n") if line.strip()]
    return bool(lines) and all(_is_data_line(line) for line in lines)


def _points_to_data(piece: str) -> bool:
    """True when a piece's own wording ends a sentence that refers to what follows it."""
    wording = piece.split("\n")[0].strip()
    if wording.startswith("###") or not re.search(r"[.:;]$", wording):
        return False
    last_sentence = re.split(r"(?<=[.;:])\s+(?=[A-Z0-9(])", wording)[-1]
    return bool(_POINTS_FORWARD.search(last_sentence))


def _join_piece(entries: list[tuple[bool, str]]) -> str:
    """The text of one piece from its (is_row, text) entries.

    Prose lines are joined with spaces, exactly as before. Rows of a table attached to a
    clause each keep a line of their own — flattened into the sentence, "Urban |
    5,000,000 | Regional Head Rural | 2,000,000 | Area Manager" no longer says which
    limit belongs to which category, and neither a reviewer nor the model can recover it.

    A piece that IS a single row keeps the old space-joined form, so an annexure row
    clause is byte-for-byte what it was.
    """
    if not entries:
        return ""
    if entries[0][0]:
        return " ".join(text for _, text in entries).strip()
    lines, previous_row = [], False
    for is_row, text in entries:
        if is_row or previous_row or not lines:
            lines.append(text)
        else:
            lines[-1] = f"{lines[-1]} {text}"
        previous_row = is_row
    return "\n".join(line.strip() for line in lines).strip()


def split_table(clause_text: str) -> tuple[str, list[str]]:
    """(the clause's own wording, the lines attached under it).

    Every line AFTER the first is attached content. A clause only spans lines when
    something was attached under it — a table's rows, a paragraph after that table, or the
    data lines of an unmarked PDF or OCR table — because its own wording is always joined
    onto one line. Looking for a cell separator instead missed the PDF case entirely, where
    the columns never had one. The first line is always wording, even when it contains "|"
    — an annexure row clause is one line with separators in it, and it is a sentence.

    Used wherever the two need different treatment: obligations are split on the wording
    and each carries the whole table, and a drafted test is written from the wording with
    the table appended rather than chopped into the sentence.
    """
    lines = [line.strip() for line in (clause_text or "").split("\n")]
    return lines[0], [line for line in lines[1:] if line]


# ====== STRUCTURAL NOISE ======
#
# What a splitter may drop on its own authority: text that is recognisably NOT something a
# regulator wrote as an instruction — page numbers, salutations and sign-offs, signature
# markers, contents entries, and headings. Recognised by SHAPE, never by what the words say.
#
# This replaces a 60-character length rule, which dropped short sentences stating a duty,
# and the keyword override that then kept only the short ones containing "shall" or
# "must". "Agents are to be verified through NADRA." has neither, and a keyword gate removed
# it before the judge — the only stage able to read it — ever saw it. Everything that is not
# noise now reaches the judge, and everything that IS noise is logged and judged once more
# (`segment_document`), so a wrong rule shows up as a rescue instead of as nothing at all.

HEADING_MAX_WORDS = 12

_SENTENCE_END = re.compile(r"""[.;:!?]["'”’)\]]*$""")
_TITLE_SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into",
                      "of", "on", "or", "per", "the", "to", "under", "with", "vs"}
_PAGE_FURNITURE = re.compile(r"^(page\s*)?\d{1,4}(\s*(of|/)\s*\d{1,4})?$|^[-–]\s*\d{1,4}\s*[-–]$",
                             re.IGNORECASE)
_SIGNATURE_MARK = re.compile(r"^(\(?-?\s*sd\s*-?\)?\s*)+$", re.IGNORECASE)
_SALUTATION = re.compile(r"^(dear|respected)\b[^.!?]{0,60}$", re.IGNORECASE)
_SIGN_OFF = re.compile(r"^(yours\b[\w\s,.]{0,25}|(best |kind |warm )?regards|sincerely|"
                       r"thanking you)[,.!]?$", re.IGNORECASE)
_CONTENTS_ENTRY = re.compile(r"^(?P<title>.*?\S)[\s.]+(?P<page>\d{1,3})$")
# The header lines the email readers write in front of a message body.
_EMAIL_HEADER = re.compile(r"^(subject|from|to|cc|bcc|date|sent)\s*:\s", re.IGNORECASE)


def _looks_like_heading(text: str) -> bool:
    """A short line with no sentence ending, set in capitals or in title case.

    Shape only. "Dedicated audit staff members" is a bullet in lower case and is NOT a
    heading; "REVISED CASH RETENTION LIMITS" and "Banking Policy & Regulations Department"
    are. A line that ends a sentence is never a heading, however short — that is what keeps
    "Banks shall reconcile ATM cassettes daily." and every lead-in ending in a colon.
    """
    if not text or _SENTENCE_END.search(text):
        return False
    if len(re.findall(r"[A-Za-z0-9][\w'’.,&/()-]*", text)) > HEADING_MAX_WORDS:
        return False
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True                                 # numbers and marks: page furniture
    if sum(c.isupper() for c in letters) / len(letters) >= 0.6:
        return True
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'’]*", text)
             if w.lower() not in _TITLE_SMALL_WORDS]
    return bool(words) and all(w[0].isupper() for w in words)


def _structural_noise(text: str) -> str | None:
    """The rule that makes this fragment structural noise, or None if it is content."""
    flat = " ".join((text or "").split())
    if len(flat) < config.MIN_FRAGMENT_CHARS:
        return "too short"
    if _EMAIL_HEADER.match(flat) and len(flat.split()) <= 20:
        return "email header"
    if _PAGE_FURNITURE.match(flat):
        return "page number"
    if _SIGNATURE_MARK.match(flat):
        return "signature"
    if _SALUTATION.match(flat) and len(flat.split()) <= 6:
        return "salutation"
    if _SIGN_OFF.match(flat):
        return "sign-off"
    contents = _CONTENTS_ENTRY.match(flat)
    if contents and _looks_like_heading(contents.group("title").rstrip(":").strip()):
        return "contents entry"
    if _looks_like_heading(flat):
        return "heading"
    return None


def _block_noise(block: str) -> str | None:
    """Why a whole blank-line block is noise, or None.

    A block holding a table marker or a row is never noise — its rows are content, handled
    line by line. A block is noise only when EVERY line is; and when each line is merely
    too short on its own, the joined block is judged instead, because a sentence broken
    across two narrow OCR lines is still a sentence.
    """
    lines = [line.strip() for line in block.split("\n") if line.strip()]
    # A line carrying cell separators is a row, even when it arrives without its "[row N]"
    # marker — a cell's second paragraph after an empty one, or a multi-line Excel cell. It
    # is content here exactly as it is in `_piece_noise`; judged as a heading, ABL's
    # "New Addition | 2.1. CALL DEPOSIT RECEIPT ..." and a branch row were dropped.
    if not lines or any(_SHEET_ROW.match(line) or _TABLE_START.match(line)
                        or _TABLE_END.match(line) or " | " in line for line in lines):
        return None
    rules = [_structural_noise(line) for line in lines]
    if not all(rules):
        return None
    if all(rule == "too short" for rule in rules):
        return _structural_noise(" ".join(lines))
    return next(rule for rule in rules if rule != "too short")


def _piece_noise(piece: str, ref) -> str | None:
    """Why a finished piece is noise, or None. A piece carrying attached lines, and a table
    kept whole, are content by construction — only the absolute floor applies to them."""
    # A table row ("Kaghan Road, Balakot | KPK | PTCL | ...") is content by construction,
    # like a table kept whole: in title case it passed for a heading, and a workbook of
    # branch reference numbers lost a third of its rows.
    if ref == TABLE_REF or "\n" in piece or " | " in piece:
        return "too short" if len(piece) < config.MIN_FRAGMENT_CHARS else None
    return _structural_noise(piece)


def _emit_pending_data(pending: list, collected: list, dropped: list, as_table: bool) -> None:
    """Close a run of unmarked data lines that no clause pointed to.

    Kept together as ONE fragment — a table whose columns were lost — only when the run has
    two lines or more AND began directly under a clause (`as_table`). That second condition
    is what tells a table apart from the other runs of short lines a PDF is full of: the
    letterhead at the top of page one, the signature block after "Yours truly,", OCR scraps
    of a browser header. Grouped as tables, those became clauses of their own on almost
    every real circular tested. Any other run is handled block by block like the rest of
    the text: noise is dropped and logged, anything else is kept.
    """
    if not pending:
        return
    lines = [line for chunk, _, _ in pending for line in chunk]
    if as_table and len(lines) >= 2:
        collected.append((TABLE_REF, "\n".join(lines), pending[0][1], pending[-1][2]))
    else:
        for chunk, start, end in pending:
            noise = _block_noise("\n".join(chunk))
            if noise:
                dropped.append({"text": "\n".join(chunk), "char_start": start,
                                "char_end": end, "rule": noise})
            else:
                collected.append((None, " ".join(chunk), start, end))
    pending.clear()


def split_clauses(text: str, unstructured: bool = False) -> list[dict]:
    """Break the text into clause-sized pieces, keeping character offsets.

    `unstructured` is True for text with no structure of its own — PDF, OCR, plain text —
    and switches on the recognition of tables whose columns were lost (UNMARKED TABLES).
    Off by default: in structured text that recognition mistakes bullets for table rows.

    Returns the clauses only. `split_with_drops` also returns what was discarded.
    """
    return split_with_drops(text, unstructured)[0]


def split_with_drops(text: str, unstructured: bool = False) -> tuple[list[dict], list[dict]]:
    """(clauses, dropped): the clauses, and every fragment a rule discarded.

    Each dropped fragment is `{"text", "char_start", "char_end", "rule"}`. They are returned
    rather than thrown away because a discarded fragment leaves no other trace — the caller
    gives them one pass through the judge and logs the rest.
    """
    clauses, collected, cursor, dropped = [], [], 0, []
    blocks = re.split(r"\n\s*\n", text)

    # Whether we are between a Word table's start and end markers. Kept ACROSS blocks: a
    # cell holding an empty paragraph puts a blank line inside the table, which splits it
    # into two blocks. Reset per block, the table's end was never recognised as closing
    # anything, and the last clause of ABL's call-back confirmation table absorbed the
    # paragraph printed after the table.
    in_table = False

    # Whether the block before this one attached data lines to the last clause, so the
    # next data block continues the same table. See UNMARKED TABLES.
    data_run_open = False

    # Unmarked data blocks that no clause pointed to, held until their run ends so the
    # run can be kept as one fragment. See `_emit_pending_data`.
    pending_data: list = []
    pending_after_clause = False

    # What the previous block became — "clause" or "dropped" — so an unmarked run can tell
    # whether it sits directly under a clause, and an OCR continuation can find its sentence.
    last_block = None

    for block in blocks:
        raw_start = text.find(block, cursor)
        if raw_start == -1:
            raw_start = cursor
        cursor = raw_start + len(block)

        # Where the block's CONTENT begins, once its leading blank space is skipped.
        # Every offset below is measured from here, so it has to account for what strip()
        # is about to remove.
        block_start = raw_start + (len(block) - len(block.lstrip()))
        block = block.strip()

        if not block:
            continue

        # A block starting in lower case, directly after a clause that has not finished its
        # sentence, IS that sentence continuing — OCR leaves a blank line wherever the page
        # had a gap. Kept apart, "maintained for the purpose and signed by the officer."
        # became a clause of its own and the clause above lost its ending. Unstructured text
        # only: in Word a paragraph break is the author's.
        # Not while an unmarked table run is open: a row starting with an OCR scrap ("c-
        # 15,000,000 ...") would otherwise be glued onto the clause above the table.
        if (unstructured and not in_table and not pending_data and last_block == "clause"
                and collected
                and block[:1].islower() and "\n" not in collected[-1][1]
                and not _SENTENCE_END.search(collected[-1][1])):
            ref, piece, start, _ = collected[-1]
            collected[-1] = (ref, piece + " " + " ".join(block.split()), start,
                             block_start + len(block))
            continue

        # The rows of a table with no markers — PDF, OCR, plain text. Checked BEFORE the
        # noise rule below, because a row with its columns gone looks just like a short
        # heading. Not inside a marked table: those rows are handled as rows.
        if unstructured and not in_table and _is_data_block(block):
            data = [line.strip() for line in block.split("\n") if line.strip()]
            end = block_start + len(block)
            if (not pending_data and collected
                    and (data_run_open or _points_to_data(collected[-1][1]))):
                # Under a clause that points to it: part of that clause.
                ref, piece, start, _ = collected[-1]
                collected[-1] = (ref, piece + "\n" + "\n".join(data), start, end)
                data_run_open = True
                last_block = "clause"
            else:
                # Nothing points to it. Held until the run ends, and kept as a table only if
                # the run began directly under a clause — see `_emit_pending_data`.
                if not pending_data:
                    pending_after_clause = last_block == "clause"
                pending_data.append((data, block_start, end))
                data_run_open = False
            continue
        data_run_open = False
        _emit_pending_data(pending_data, collected, dropped, pending_after_clause)

        # Structural noise — page numbers, salutations, signatures, contents entries,
        # headings — is dropped here and logged. Never text dropped for what it SAYS; see
        # STRUCTURAL NOISE. A block holding a table marker is never noise, so table state
        # is always tracked by the line loop below.
        noise = _block_noise(block)
        if noise:
            dropped.append({"text": block, "char_start": block_start,
                            "char_end": block_start + len(block), "rule": noise})
            last_block = "dropped"
            continue

        # Split a long block on numbered openers so each obligation stands alone.
        #
        # OFFSETS ARE TRACKED HERE, NOT SEARCHED FOR AFTERWARDS. The obvious version —
        # `text.find(piece[:60], block_start)` once the piece is assembled — cannot work:
        # a piece is its lines joined with single spaces, so the moment its first 60
        # characters cross a line break the search fails and every piece in the block
        # falls back to the block's own start. On a 23-page circular that put 67 of 166
        # clauses at the wrong offset, which silently moved `page_number` and made the
        # source citation shown to a reviewer point at the wrong part of the document.
        # Walking the lines costs nothing and is exact.
        lines, current, current_ref = block.split("\n"), [], None
        current_is_row = False            # is the open piece a table row standing alone?
        table_owner = False               # are these rows part of the open clause?
        table_whole = False               # is this data table kept whole as its own clause?
        opened_in_table = False           # did the open piece START inside a table?
        pieces = []                       # (ref, text, start, end)
        piece_start = piece_end = block_start
        line_at = block_start             # absolute offset of the current line

        def flush():
            """Close the piece being built, if it has anything in it."""
            if current:
                pieces.append((current_ref, _join_piece(current), piece_start, piece_end))

        for position, line in enumerate(lines):
            lead = len(line) - len(line.lstrip())          # indent, kept out of offsets
            content_at = line_at + lead
            content_end = line_at + len(line.rstrip())
            stripped = line.strip()

            # A table is starting. Decide ONCE, for the whole table, whether it belongs to
            # the clause above — see TABLES UNDER A CLAUSE. The markers are not text: they
            # add nothing to any piece, and only their own line moves the offset.
            if _TABLE_START.match(stripped):
                is_data = _is_data_table(_table_rows(lines, position))
                table_owner = is_data and _can_own_table(current, current_is_row)
                # A data table nothing points to is kept WHOLE, as a clause of its own.
                # Split row by row, every row is short enough for the noise rule to take,
                # and the limits would vanish because no paragraph said "below".
                table_whole = is_data and not table_owner
                if table_whole:
                    flush()
                    current, current_ref, current_is_row = [], TABLE_REF, False
                in_table = True
                line_at += len(line) + 1
                continue

            # The table has ended. Anything that STARTED inside it — a row standing alone,
            # or a numbered clause from a paragraph inside a cell — is closed here, so the
            # paragraph after the table starts a piece of its own. Without this, tables now
            # being read where they sit, the section after a signature block arrived as
            # "Chief BSG | Chief CG INDIVIDUAL CP CREATION ...", and the last clause inside
            # a manual-amendment table absorbed the next chapter's heading. A clause that
            # began BEFORE the table and owns it stays open: an unnumbered paragraph after
            # it continues the clause, as unnumbered paragraphs always have.
            if _TABLE_END.match(stripped):
                table_owner = table_whole = in_table = False
                if current and opened_in_table:
                    flush()
                    current, current_ref, current_is_row = [], None, False
                line_at += len(line) + 1
                continue

            sheet_row = _SHEET_ROW.match(stripped)
            if sheet_row:
                body = sheet_row.group(2).strip()
                # The piece stays open, so the clause's span grows to cover its table and
                # the next numbered clause is still what closes it. A row that states a
                # duty of its own ends the attachment: from there the table is a list of
                # requirements, and each row stands alone.
                if table_whole:
                    if not current:
                        piece_start = content_at + sheet_row.start(2)
                        opened_in_table = True
                    # The first row is the fragment's own line; the rest are attached
                    # under it, each on a line of its own.
                    current.append((bool(current), body))
                    piece_end = content_end
                    line_at += len(line) + 1
                    continue
                if table_owner and not _states_a_duty(body):
                    current.append((True, body))
                    piece_end = content_end
                    line_at += len(line) + 1
                    continue
                table_owner = False
                flush()
                current_ref = _row_ref(sheet_row.group(1), sheet_row.group(2))
                current, current_is_row = [(True, body)], True
                opened_in_table = in_table
                # Point past the "[row 4] " marker at the row's actual content.
                piece_start = content_at + sheet_row.start(2)
                piece_end = content_end
                line_at += len(line) + 1
                continue

            # A second paragraph inside a cell of a table kept whole belongs to that row.
            if table_whole and current:
                current[-1] = (current[-1][0], f"{current[-1][1]} {stripped}")
                piece_end = content_end
                line_at += len(line) + 1
                continue

            # A second paragraph inside a cell of an attached table belongs to that row,
            # not to the clause and not to a piece of its own.
            if table_owner and current and current[-1][0]:
                current[-1] = (True, f"{current[-1][1]} {stripped}")
                piece_end = content_end
                line_at += len(line) + 1
                continue

            # A row of an UNMARKED table in the same block as the clause pointing to it.
            # Native PDF text puts no blank line between a clause and the table under it, so
            # the rows arrive as ordinary lines and were joined onto the clause with spaces:
            # the limits survived inside the clause's sentence, but not as lines of their
            # own, so the drafted test — which carries a clause's attached lines — never saw
            # them. A wrapped line of the clause itself is not taken: until the wording ends
            # a sentence that points forward, the next line is still that sentence.
            if (unstructured and not in_table and current and not current_is_row
                    and _is_data_line(stripped)
                    and (current[-1][0] or _points_to_data(_join_piece(current)))):
                current.append((True, stripped))
                piece_end = content_end
                line_at += len(line) + 1
                continue

            table_owner = False
            match = _NUMBERED.match(line)
            if match and current:
                flush()
                current, current_ref = [], match.group(1).strip(".) ")
                piece_start = content_at
            elif match:
                current_ref = match.group(1).strip(".) ")
                if not current:
                    piece_start = content_at
            elif not current:
                piece_start = content_at

            if not current:
                current_is_row = False
                opened_in_table = in_table
            current.append((False, stripped))
            piece_end = content_end
            line_at += len(line) + 1       # +1 for the newline split() removed

        flush()
        collected.extend(pieces)
        if pieces:
            last_block = "clause"

    # Lists are joined to the sentence that introduces them before anything is measured
    # — an item is often short on its own and only reaches a sensible length once it
    # carries its stem.
    _emit_pending_data(pending_data, collected, dropped, pending_after_clause)

    for ref, piece, start, end in _attach_lead_ins(collected):
        noise = _piece_noise(piece, ref)
        if noise:
            dropped.append({"text": piece, "char_start": start,
                            "char_end": max(end, start + 1), "rule": noise})
            continue
        clauses.append({
            "clause_ref": ref,
            "text": piece,
            "char_start": start,
            # The measured end of the source span, NOT start + len(piece). A piece is its
            # lines joined with spaces, so its length only approximates the span it came
            # from — and an approximate end makes every coverage figure approximate too.
            "char_end": max(end, start + 1),
            "table_unattached": ref == TABLE_REF,
        })

    return clauses, dropped


def classify(clause_text: str) -> tuple[bool, str, str]:
    """(is_actionable, strata_tag, reason)."""
    head = clause_text.strip()[:160]

    if _INFORMATIONAL.match(head):
        return False, "", "Covering or reference text — for information only"

    if _SUPERSESSION.search(clause_text):
        return True, _strata_for(clause_text), "Withdraws or supersedes earlier instructions"

    if _OBLIGATION.search(clause_text):
        return True, _strata_for(clause_text), "Creates an obligation on the bank"

    return False, "", "No obligation wording found — for information only"


# ====== THE SAME JUDGEMENT, MADE BY A MODEL ======
#
# Keyword rules are shallow in both directions. "Banks shall be informed in due course"
# carries `shall` and imposes nothing; "Agents are to be verified through NADRA prior to
# onboarding" is a hard obligation with no keyword in it at all. Passive constructions
# miss constantly, and a sentence QUOTING an older circular reads exactly like one
# imposing a new duty.
#
# So the model reads the clause. Clauses go in batches — one call per clause would be
# 166 calls for a single circular — and the batch is small enough that a 7B model can
# hold all of it and still answer in order.

JUDGE_BATCH = 8
JUDGE_TIMEOUT = 180

# What one clause may contribute to a judging batch. A 7B model's context is finite and
# eight full clauses will not fit, so long clauses are shortened HERE — and the clause
# row records that it happened. The real system faces the same question at the embedding
# step, where bge-large silently cuts at 512 tokens; there the answer is overlapping
# windows indexed back to the parent clause, because the tail of a long obligation is
# usually the part that identifies the right test.
JUDGE_INPUT_CHARS = 900

_JUDGE_PROMPT = """You are helping a bank's internal audit team read a regulatory circular.

For each numbered clause, decide whether it creates an obligation the bank can be
AUDITED against.

actionable = true when the clause requires, forbids, or withdraws something — including
  wording with no "shall" in it, such as "agents are to be verified before onboarding".
actionable = false for headings, definitions, covering text, background, salutations,
  and for a clause that merely QUOTES or refers to an earlier instruction without
  imposing anything new.

Set supersedes = true when the clause withdraws, supersedes, cancels or ends an earlier
instruction, however indirectly worded. This matters more than the rest: a withdrawal
that reads as ordinary prose is the most damaging thing to miss.

CLAUSES:
{clauses}

Reply with ONE JSON object of the form {{"verdicts": [ ... ]}} containing EXACTLY
{count} entries, one per clause, in the same order. `reason` is your own words about
THAT clause, at most six of them — never copy the example:

{{"verdicts": [
  {{"n": 1, "actionable": true, "supersedes": false, "reason": "requires board-approved policy"}},
  {{"n": 2, "actionable": false, "supersedes": false, "reason": "heading only"}}
]}}"""


def _judge_batch(texts: list[str]) -> list[dict] | None:
    """Ask the model about several clauses at once. None once every attempt is used up.

    Retried up to `config.LLM_ATTEMPTS` times. Both failure modes are worth retrying and
    for the same reason — a 7B model's bad reply is usually a bad SAMPLE, not a bad
    prompt, so the next attempt often succeeds on identical input:

    * the call itself failed — a timeout, a dropped connection, an HTTP error
    * the reply came back unusable — not JSON, or the wrong number of verdicts, which
      `_parse_verdicts` refuses rather than padding

    Every retry is printed. A run that silently retried twice and then fell back looks
    exactly like one that worked first time, and the difference is what tells you whether
    the model is healthy.
    """
    import httpx

    listing = "\n\n".join(f"{i}. {t[:JUDGE_INPUT_CHARS]}"
                           for i, t in enumerate(texts, start=1))
    payload = {"model": config.OLLAMA_MODEL, "stream": False, "format": "json",
               "messages": [{"role": "user",
                             "content": _JUDGE_PROMPT.format(
                                 clauses=listing, count=len(texts))}],
               "options": {"temperature": 0}}

    for attempt in range(1, config.LLM_ATTEMPTS + 1):
        why = ""
        try:
            response = httpx.post(f"{config.OLLAMA_URL}/api/chat", json=payload,
                                  timeout=JUDGE_TIMEOUT)
            response.raise_for_status()
            verdicts = _parse_verdicts(response.json()["message"]["content"], len(texts))
            if verdicts is not None:
                if attempt > 1:
                    print(f"   model call succeeded on attempt {attempt}")
                return verdicts
            why = f"reply unusable — expected {len(texts)} verdicts"
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}"

        if attempt < config.LLM_ATTEMPTS:
            wait = config.LLM_RETRY_BACKOFF * (2 ** (attempt - 1))
            print(f"   model call failed (attempt {attempt}/{config.LLM_ATTEMPTS}): "
                  f"{why[:90]} — retrying in {wait:.0f}s")
            time.sleep(wait)
        else:
            print(f"   model call failed on all {config.LLM_ATTEMPTS} attempts: "
                  f"{why[:90]} — falling back to the rules for this batch")
    return None


def _parse_verdicts(content: str, expected: int) -> list[dict] | None:
    """Read the verdicts out of whatever shape the model replied in.

    A 7B model returns any of: a JSON array; a single bare object when the batch held
    one clause; several objects back to back with no array around them; or an object
    wrapping the array under some key of its own. All four are the same answer, so all
    four are read — but a reply with the WRONG NUMBER of verdicts is refused rather than
    padded, because a guess about which clause a verdict belongs to is worse than
    falling back to the rules.
    """
    values, decoder, index = [], json.JSONDecoder(), 0
    while index < len(content):
        start = content.find("{", index)
        bracket = content.find("[", index)
        if bracket != -1 and (start == -1 or bracket < start):
            start = bracket
        if start == -1:
            break
        try:
            value, end = decoder.raw_decode(content, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        values.append(value)
        index = end

    verdicts: list[dict] = []
    for value in values:
        if isinstance(value, list):
            verdicts.extend(v for v in value if isinstance(v, dict))
        elif isinstance(value, dict):
            nested = next((v for v in value.values() if isinstance(v, list)), None)
            if nested is not None:
                verdicts.extend(v for v in nested if isinstance(v, dict))
            elif "actionable" in value:
                verdicts.append(value)

    return verdicts if len(verdicts) == expected else None


def classify_llm(texts: list[str]) -> list[tuple[bool, str, str, str]]:
    """Judge a list of clauses. Returns (actionable, strata, reason, judged_by) each.

    Falls back to the keyword rules PER BATCH when the model is unreachable or answers
    unusably, and says so in `judged_by` — a demo that quietly degrades to rules while
    claiming to use a model is worse than one that never had the model.
    """
    out: list[tuple[bool, str, str, str]] = []

    for start in range(0, len(texts), JUDGE_BATCH):
        chunk = texts[start:start + JUDGE_BATCH]
        answers = _judge_batch(chunk)

        if answers is None:
            for text in chunk:
                actionable, strata, reason = classify(text)
                out.append((actionable, strata, reason, "rules (model unavailable)"))
            continue

        for text, answer in zip(chunk, answers):
            if not isinstance(answer, dict):
                actionable, strata, reason = classify(text)
                out.append((actionable, strata, reason, "rules (unusable answer)"))
                continue
            actionable = bool(answer.get("actionable"))
            supersedes = bool(answer.get("supersedes"))
            reason = str(answer.get("reason") or "")[:120]
            if supersedes:
                # A withdrawal is always actionable, whatever else the model said.
                actionable = True
                reason = reason or "Withdraws or supersedes earlier instructions"
            out.append((actionable, _strata_for(text) if actionable else "",
                        reason or "Judged by the model", "llm"))
    return out


def _strata_for(text: str) -> str:
    lowered = text.lower()
    for strata, pattern in _STRATA_HINTS:
        if re.search(pattern, lowered):
            return strata
    return "Account Operation"


def is_supersession(text: str) -> bool:
    """True when the clause withdraws or supersedes an earlier instruction.

    Checked on EVERY clause and before the similarity thresholds, because a withdrawal
    must never depend on a score. Implicit deletions are the most damaging miss in this
    system: circulars almost never say "delete test X".
    """
    return bool(_SUPERSESSION.search(text))


JUDGES = {"rules": "keyword rules", "llm": "local model"}

# ====== OBLIGATIONS WITHIN ONE CLAUSE ======
#
# One sentence often carries several duties, each matching a different audit test. The
# clause stays whole — it is the unit of traceability — but the duties are pulled out so
# that each can be its own query and its own proposal.
#
# The rules engine only handles the EXPLICIT case: duties joined by "and shall",
# "; shall", ", and must". That is narrow on purpose. Deciding where one duty ends and
# the next begins is a language judgement, and in the delivered system the model returns
# the list directly — see prompt 2.3. A keyword split that tried to be clever here would
# fragment ordinary prose, which is worse than not splitting at all.

MAX_OBLIGATIONS_PER_CLAUSE = 6      # more than this means we are fragmenting a paragraph
MIN_OBLIGATION_CHARS = 40           # shorter than this is a fragment, not a duty

# A coordinating conjunction followed by a duty verb. Requiring the verb is what keeps
# "agents and branches shall be verified" (one duty) from being split in half.
_OBLIGATION_JOIN = re.compile(
    r"(?:,\s*and|;\s*and|,|;)\s+(?=(?:shall|must|may not|is required to|are required to)\b)",
    re.IGNORECASE)


def split_obligations(clause_text: str) -> list[str]:
    """The distinct duties in one clause. Returns [clause_text] when there is only one.

    Deterministic and deliberately narrow — see the note above. Every returned duty is
    a complete sentence a reviewer can read on its own, because it becomes the retrieval
    query and the wording of a proposed test.
    """
    text = clause_text.strip()
    # Split the WORDING, and give every duty the whole table. A table under a clause
    # parameterises the clause, not one sentence of it — split naively, the rows ride
    # along on the last duty only, and the first duty's proposed test loses its limits.
    wording, rows = split_table(text)
    table = "\n".join(rows)
    parts = [p.strip(" ,;") for p in _OBLIGATION_JOIN.split(wording) if p.strip(" ,;")]
    if len(parts) < 2:
        return [text]

    # Carry the subject onto the later duties. "Banks shall verify X, and shall maintain
    # Y" gives "shall maintain Y" — true to the source but useless as a query, because
    # the subject is what the retrieval needs to match a branchless-banking test.
    subject = wording.split(" shall")[0].split(" must")[0].strip()
    # Drop a leading list marker — "e) The Bank" carries "e)" onto every later duty, and
    # the marker belongs to the clause reference, not to the sentence.
    subject = _LIST_ITEM.sub("", subject).strip()
    if len(subject) > 60:                       # a whole preamble, not a subject
        subject = ""
    out = [parts[0]]
    for part in parts[1:]:
        out.append(f"{subject} {part}".strip() if subject else part)

    out = [p for p in out if len(p) >= MIN_OBLIGATION_CHARS]
    if len(out) < 2:
        return [text]
    return [f"{duty}\n{table}" if table else duty
            for duty in out[:MAX_OBLIGATIONS_PER_CLAUSE]]


# ====== SANITY CHECKS OVER THE SPLIT ======
#
# The patterns above are written against the documents we have. A circular with no
# numbering, a two-column layout, or OCR that lost the indentation will not raise an
# error — it will return one enormous clause or hundreds of fragments, and every number
# downstream degrades quietly. These checks make that visible. They FLAG the document;
# they never fail it.

# A clause past this length is a whole section that arrived as one block. It still
# works, but it retrieves poorly — the specific requirement is diluted by everything
# around it — so it is worth flagging rather than silently accepting.
MAX_SENSIBLE_CLAUSE = 4000

# Below this median, the document is almost certainly fragmented: line breaks in a
# two-column or OCR'd layout being read as clause boundaries. Real obligations run
# well over 80 characters. Only applied where there are enough clauses for a median
# to mean anything — see check_split().
MIN_SENSIBLE_MEDIAN = 80


def check_split(clauses: list[dict], text: str) -> str:
    """Return a warning about this document's split, or "" if it looks reasonable."""
    if not clauses:
        return "no clauses found at all"

    lengths = sorted(len(c["text"]) for c in clauses)
    median = lengths[len(lengths) // 2]
    problems = []

    # ~2,500 characters is roughly a page of a circular.
    if len(clauses) == 1 and len(text) > 5 * 2500:
        problems.append("one clause for a document of %d characters — no separator was "
                        "recognised" % len(text))
    if len(clauses) > 100 and median < 60:
        problems.append("%d clauses with a median of %d characters — this looks like a "
                        "two-column or OCR'd layout split on line breaks"
                        % (len(clauses), median))
    # A median is only meaningful over enough clauses. A spreadsheet annexure with four
    # genuinely short requirements is not fragmented, and flagging it would train the
    # reader to ignore the warning — which costs more than the check is worth.
    elif len(clauses) >= 10 and median < MIN_SENSIBLE_MEDIAN:
        problems.append("median clause is %d characters — the obligations are probably "
                        "in fragments" % median)
    oversized = [c for c in clauses if len(c["text"]) > MAX_SENSIBLE_CLAUSE]
    if oversized:
        problems.append("%d clause(s) over %d characters — a whole section arrived as one "
                        "block and will retrieve poorly" % (len(oversized), MAX_SENSIBLE_CLAUSE))
    return "; ".join(problems)


def _split_with_fallback(text: str,
                         unstructured: bool = False) -> tuple[list[dict], str, list[dict]]:
    """Split the text into clauses, and say WHICH rung of the ladder produced them.

    Rung 1 is structure — numbering, headings, table rows, spreadsheet rows — and is what
    almost every document hits. Rung 2 exists because a short circular (a one-page RIA
    instruction, an OCR'd image, a single spreadsheet row) can fall under the paragraph
    threshold and yield nothing at all, and losing a whole document to a length rule is
    worse than keeping a rough clause.

    The rung is returned, not hidden, because a fallback split must never be
    indistinguishable from a structural one.
    """
    found, dropped = split_with_drops(text, unstructured)
    found = found[: config.MAX_CLAUSES_PER_DOC]
    if found or not text.strip():
        return found, "structure", dropped

    # Rung 2 — obligation-bearing lines, for a short instruction or OCR output with no
    # recoverable numbering.
    lines = [ln.strip() for ln in text.split(chr(10)) if len(ln.strip()) > 25]
    cursor = 0
    for line in lines[: config.MAX_CLAUSES_PER_DOC]:
        start = text.find(line, cursor)
        cursor = (start if start != -1 else cursor) + len(line)
        found.append({"clause_ref": None, "text": line,
                      "char_start": max(start, 0),
                      "char_end": max(start, 0) + len(line)})
    # Rung 2 keeps every line, so nothing it saw was dropped — no keyword decides which.
    return found, "obligation-lines", []


def _judge_clauses(found: list[dict], judge: str) -> list[tuple]:
    """Decide actionability for every clause: one model call per batch, or the rules.

    Returns one (actionable, strata, reason, judged_by) tuple per clause, in the same
    order. `judged_by` travels with each verdict rather than being assumed from `judge`,
    because a batch can fall back to the rules on its own when the model is unreachable.
    """
    if judge == "llm" and found:
        return classify_llm([c["text"] for c in found])
    return [(*classify(c["text"]), "rules") for c in found]


def _store_clause(conn, document_id: int, sequence: int, clause: dict, verdict: tuple,
                  rung: str, chars_per_page: int) -> int:
    """Write one clause row and return its id.

    A verdict from the rules standing in for an unreachable model is stored with
    `is_actionable` NULL — PROVISIONAL, not a decision — and `rejudge_provisional` asks the
    model again on the next run. Stored as 1 or 0, a keyword verdict made on the day the
    model was down would quietly become the permanent answer.
    """
    actionable, strata, reason, judged_by = verdict
    return store.insert(conn, "clauses", {
        "document_id": document_id,
        "clause_ref": clause["clause_ref"] or f"para {sequence}",
        "sequence": sequence,
        "text": clause["text"],
        "page_number": clause["char_start"] // chars_per_page + 1,
        "char_start": clause["char_start"],
        "char_end": clause["char_end"],
        "is_actionable": None if is_provisional(judged_by) else (1 if actionable else 0),
        "strata_tag": strata,
        "reason": reason,
        "judged_by": judged_by,
        "segmented_by": rung,
        "model_input_truncated": 1 if len(clause["text"]) > JUDGE_INPUT_CHARS else 0,
        "rescued_by": clause.get("rescued_by"),
        "table_unattached": 1 if clause.get("table_unattached") else 0,
    })


def _store_obligations(conn, clause_id: int, clause_text: str, strata: str) -> list[str]:
    """Pull the separate duties out of one clause and store them. Returns the duties.

    A clause routinely states several duties in one sentence, and each is its own audit
    test, its own retrieval query and its own proposal. The clause text is never split —
    it stays the unit of traceability — the duties are recorded against it.
    """
    duties = split_obligations(clause_text)
    for order, duty in enumerate(duties, start=1):
        store.insert(conn, "obligations", {
            "clause_id": clause_id,
            "sequence": order,
            "text": duty,
            "strata_tag": _strata_for(duty) or strata,
            "split_by": "rules",
        })
    return duties


# ====== DROPPED TEXT GETS A SECOND READ ======

def _rescue_dropped(dropped: list[dict], judge: str) -> tuple[list[tuple], list[dict]]:
    """Judge every discarded fragment once more. Returns (rescued, still dropped).

    A splitting rule that removes text the judge would have called an obligation fails in
    silence: no clause, no proposal, no warning. So everything a STRUCTURAL rule dropped is
    judged once — with the same judge as the rest of the document — and anything found to
    be an obligation comes back as a clause marked `rescued_by`. Fragments under the absolute
    floor are not re-read: they are too short to be read as anything.

    With the rules judge a keyword brings a fragment BACK. That is a keyword adding to what
    is read — the one direction a keyword check is allowed to act in.
    """
    candidates = [d for d in dropped if d["rule"] != "too short"]
    kept = [d for d in dropped if d["rule"] == "too short"]
    if not candidates:
        return [], dropped
    # A dropped block can span lines; as a clause it is one sentence, not a table.
    clauses = [{"clause_ref": None, "text": " ".join(d["text"].split()),
                "char_start": d["char_start"], "char_end": d["char_end"]}
               for d in candidates]
    rescued = []
    for fragment, clause, verdict in zip(candidates, clauses, _judge_clauses(clauses, judge)):
        if verdict[0]:
            clause["rescued_by"] = f"{verdict[3]} (dropped as {fragment['rule']})"
            rescued.append((clause, verdict))
        else:
            kept.append(fragment)
    return rescued, sorted(kept, key=lambda d: d["char_start"])


def _merge_in_order(found: list[dict], verdicts: list[tuple],
                    rescued: list[tuple]) -> tuple[list[dict], list[tuple]]:
    """Put rescued fragments back where they sit, so sequence and page order stay true."""
    if not rescued:
        return found, verdicts
    pairs = sorted(list(zip(found, verdicts)) + rescued, key=lambda p: p[0]["char_start"])
    return [p[0] for p in pairs], [p[1] for p in pairs]


# ====== PROVISIONAL VERDICTS ======
#
# When the model cannot be reached, `classify_llm` answers with the keyword rules and says
# so in `judged_by`. That answer is PROVISIONAL: stored with `is_actionable` NULL, still
# turned into flagged proposals (a clause waiting silently for the next run is invisible),
# and asked of the model again on every later run until the model answers.

# The `judged_by` labels a fallback writes, and the one a clause gets when a reviewer acted
# before the model could be asked. All start with "rules (".
_REJUDGE_LABELS = ("rules (model unavailable)", "rules (unusable answer)")
_KEPT_LABEL = "rules (kept: a reviewer acted before the model could judge)"


def is_provisional(judged_by: str | None) -> bool:
    """True for a verdict the rules gave in the model's place."""
    return bool(judged_by) and judged_by.startswith("rules (")


def rejudge_provisional(conn) -> dict:
    """Ask the model again about every clause a fallback judged. Returns what happened.

    For each queued clause the model's verdict replaces the provisional one, and the
    clause's obligations and proposals are cleared so stage 4 proposes afresh — unless a
    human has already acted on a proposal from that clause. That clause is left exactly as
    it is, relabelled so it is not queued for ever, and counted.

    Returns {"queued", "confirmed", "still_provisional", "kept_human": counts,
    "documents": ids of documents whose clauses changed}.
    """
    rows = conn.execute(
        "SELECT id, document_id, text FROM clauses WHERE is_actionable IS NULL "
        "AND judged_by IN (?, ?) ORDER BY document_id, sequence", _REJUDGE_LABELS).fetchall()
    summary = {"queued": len(rows), "confirmed": 0, "still_provisional": 0,
               "kept_human": 0, "documents": []}
    if not rows:
        return summary

    for row, verdict in zip(rows, classify_llm([r["text"] for r in rows])):
        actionable, strata, reason, judged_by = verdict
        if is_provisional(judged_by):
            summary["still_provisional"] += 1
            continue
        if not store.clear_clause_work(conn, row["id"]):
            conn.execute("UPDATE clauses SET judged_by = ? WHERE id = ?",
                         (_KEPT_LABEL, row["id"]))
            summary["kept_human"] += 1
            continue
        conn.execute("UPDATE clauses SET is_actionable = ?, strata_tag = ?, reason = ?, "
                     "judged_by = ? WHERE id = ?",
                     (1 if actionable else 0, strata, reason, judged_by, row["id"]))
        if actionable:
            _store_obligations(conn, row["id"], row["text"], strata)
        summary["confirmed"] += 1
        if row["document_id"] not in summary["documents"]:
            summary["documents"].append(row["document_id"])
    return summary


def segment_document(conn, document_id: int, text: str, judge: str = "rules",
                     unstructured: bool = False) -> dict:
    """Split one document into clauses, judge each, and store them.

    Five steps, in order:
        1. split          -> clauses, which rung produced them, and what was dropped
        2. judge          -> is this clause an obligation the bank can be audited against
        3. rescue         -> judge the dropped text once more; restore any obligation found
        4. sanity-check   -> flag a split that looks wrong; never fail the document for it
        5. store          -> one clause row each, its obligations, and the drops logged

    Returns what the caller needs to report the document —
    `{"clauses": n, "actionable": n, "warning": str or None}` — rather than just a count.
    All three are already known here: the clause list, the verdicts and the warning were
    computed above. Returning only the count forced the caller to run two SELECTs to
    recover facts this function had in hand, which put schema knowledge in the pipeline
    script for the sake of one printed line.
    """
    # This document may already have clauses — an interrupted run, or two runs going at
    # once. Inserting on top of them produces a SECOND full set: every clause duplicated,
    # each with its own proposal, so the reviewer sees every row twice under two different
    # Sr numbers. Nothing errors, because nothing forbids it.
    #
    # So clear the previous attempt first, and refuse if a human has already acted on any
    # of it — reproducible work can be redone, a reviewer's decision cannot.
    existing = store.clause_count(conn, document_id)
    if existing and not store.clear_document_work(conn, document_id):
        raise ValueError(
            f"document {document_id} already has {existing} clauses and someone has "
            f"acted on its proposals — refusing to segment it again. Investigate before "
            f"re-running; a duplicate set would put every clause in the queue twice.")

    found, rung, dropped = _split_with_fallback(text, unstructured)
    verdicts = _judge_clauses(found, judge)
    rescued, dropped = _rescue_dropped(dropped, judge)
    found, verdicts = _merge_in_order(found, verdicts, rescued)

    # A split that looks wrong FLAGS the document; it never fails it. Someone has to
    # notice, and a low clause count three screens later is not noticing.
    warning = check_split(found, text)
    if warning:
        conn.execute("UPDATE documents SET segment_warning = ? WHERE id = ?",
                     (warning, document_id))

    _debug_split(document_id, text, found, verdicts, rung, warning)
    _debug_drops(dropped, rescued)

    chars_per_page = max(len(text) // max(_pages(conn, document_id), 1), 1)
    for fragment in dropped:
        store.insert(conn, "dropped_fragments", {"document_id": document_id, **fragment})

    for sequence, (clause, verdict) in enumerate(zip(found, verdicts), start=1):
        clause_id = _store_clause(conn, document_id, sequence, clause, verdict,
                                  rung, chars_per_page)
        # Obligations are stored whenever the verdict found a duty — a PROVISIONAL verdict
        # included, so the clause still reaches a reviewer, flagged, instead of waiting
        # unseen for the model to come back.
        if verdict[0]:
            duties = _store_obligations(conn, clause_id, clause["text"], verdict[1])
            _debug_obligations(clause["clause_ref"] or f"para {sequence}",
                               clause["text"], duties)
    return {"clauses": len(found),
            "actionable": sum(1 for v in verdicts if v[0]),
            "rescued": len(rescued),
            "dropped": len(dropped),
            "warning": warning}


def _debug_split(document_id, text, found, verdicts, rung, warning) -> None:
    """Print what the splitter produced for one document, and what it left behind."""
    print(f"[SEGMENT] document {document_id}: {len(text):,} chars -> "
          f"{len(found)} clauses (rung={rung})")
    if warning:
        print(f"[SEGMENT]   CHECK THE SPLIT: {warning}")

    # How much of the text ended up inside a clause. This is the number that catches
    # content going missing: 40 good clauses out of a document that should have yielded
    # 60 looks exactly like 40 out of 40 in any count on any screen.
    # Spans overlap (a lead-in is carried by every list item), so merge, do not sum.
    merged = []
    for s, e in sorted((c["char_start"], c["char_end"]) for c in found):
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    covered = sum(e - s for s, e in merged)
    print(f"[SEGMENT]   text captured in clauses: "
          f"{100.0 * covered / max(len(text), 1):.1f}%  ({covered:,} of {len(text):,})")

    # The longest stretches that became no clause at all — look here first.
    cursor, gaps = 0, []
    for s, e in merged:
        if s - cursor > 200:
            gaps.append((cursor, s))
        cursor = max(cursor, e)
    if len(text) - cursor > 200:
        gaps.append((cursor, len(text)))
    for s, e in sorted(gaps, key=lambda g: g[1] - g[0], reverse=True)[:5]:
        excerpt = text[s:e][:120].replace("\n", " / ")
        print(f"[SEGMENT]   NOT IN ANY CLAUSE chars {s:,}-{e:,} ({e - s:,}): {excerpt}")

    for i, (c, v) in enumerate(zip(found, verdicts), start=1):
        print(f"[SEGMENT]   {i:>3}. [{(c['clause_ref'] or '--'):>9}] "
              f"{'ACTIONABLE' if v[0] else 'info      '} {len(c['text']):>5}ch "
              f"{(v[1] or '-'):<20} " + c["text"][:70].replace("\n", " "))


def _debug_drops(dropped: list[dict], rescued: list[tuple]) -> None:
    """Print what the splitter discarded, by rule, and what the judge brought back."""
    for clause, _ in rescued:
        print(f"[SEGMENT]   RESCUED ({clause['rescued_by']}): " + clause["text"][:90])
    by_rule: dict[str, int] = {}
    for fragment in dropped:
        by_rule[fragment["rule"]] = by_rule.get(fragment["rule"], 0) + 1
    if by_rule:
        print("[SEGMENT]   dropped as noise: "
              + ", ".join(f"{n} {rule}" for rule, n in sorted(by_rule.items())))


def _debug_obligations(ref, clause_text, duties) -> None:
    """Print multi-duty clauses, and clauses that look like one was missed."""
    verbs = len(_OBLIGATION.findall(clause_text))
    if len(duties) > 1:
        print(f"[OBLIGATIONS] [{ref}] {len(duties)} duties from one clause:")
        for n, d in enumerate(duties, start=1):
            print(f"[OBLIGATIONS]     {n}. " + d[:100].replace("\n", " "))
    elif verbs > 1:
        # Several obligation verbs but only one duty found: a joining phrase the splitter
        # does not know, and the second duty never gets a proposal.
        print(f"[OBLIGATIONS] [{ref}] {verbs} obligation verbs but only ONE "
              f"duty — join missed? " + clause_text[:90].replace("\n", " "))


def _pages(conn, document_id: int) -> int:
    row = conn.execute("SELECT pages FROM documents WHERE id = ?", (document_id,)).fetchone()
    return (row["pages"] or 1) if row else 1
