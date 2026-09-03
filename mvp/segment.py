"""Split a document into clauses and judge which ones create an obligation.

Splitting is deterministic — numbering and paragraph breaks, no model. The actionability
judgement is the part that needs language understanding; in the real system that is an
LLM call, and here it is a keyword rule so the demo runs instantly. The UI says so.
"""

import re

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


def _split_list_items(piece: str) -> tuple[str, list[str]]:
    """Break "The following apply: a) ... b) ..." into (stem, items).

    The stem is whatever precedes the first marker in the same block — often the whole
    sentence that makes the items testable — and is returned separately so it can be
    carried onto each item rather than surviving as a clause of its own. Returns
    ("", []) when the text is not a list.
    """
    for pattern in (r"(?=(?<![A-Za-z0-9])[a-z][)]\s)",       # a) b) c)
                    r"(?=(?<![A-Za-z0-9])[ivx]{1,5}[).]\s)",  # i) ii) iii)
                    r"(?=(?<![A-Za-z0-9])\d{1,2}[)]\s)",      # 1) 2) 3)
                    r"(?=[•▪]\s)"):
        parts = [p.strip() for p in re.split(pattern, piece) if p.strip()]
        items = [p for p in parts
                 if _LIST_ITEM.match(p) and len(p) >= _MIN_LIST_ITEM_CHARS]

        # Two items make a list; one match is far more likely to be prose.
        if len(items) < 2:
            continue
        first = _LIST_ITEM.match(items[0])
        if first and (first.group("mark") or "") not in _OPENERS:
            continue                                  # starts mid-sequence: a quotation

        stem = parts[0] if parts and not _LIST_ITEM.match(parts[0]) else ""
        return stem, items
    return "", []


def _attach_lead_ins(pieces: list[tuple]) -> list[tuple]:
    """Give every list item the stem that introduces it, and drop the bare stem.

    `pieces` is [(ref, text, start)] in document order; the same shape comes back.
    """
    stems: list[tuple[int, int, str]] = []          # (source index, start, text)
    out: list[tuple] = []                           # (ref, text, start, source index)
    consumed: set[int] = set()

    for index, (ref, piece, start) in enumerate(pieces):
        inline_stem, items = _split_list_items(piece)

        # The stem in reach, if there is one: the most recent, and only while it is
        # close enough to still be the sentence that opened this list.
        pending = None
        if stems and start - stems[-1][1] <= _LEAD_IN_LOOKBACK:
            pending = stems[-1]

        if items:
            # A stem in the same block wins: it is unambiguously this list's sentence.
            stem_text = inline_stem
            if not stem_text and pending:
                stem_text = pending[2]
                consumed.add(pending[0])
            for item in items:
                out.append((ref, f"{stem_text} {item}" if stem_text else item,
                            start, index))
            continue

        # A list whose items are split across blocks — a page break falls between "a)"
        # and "b)", so each arrives alone and the two-item rule above never fires. A
        # marker at the very START of a block is a safe enough signal on its own; the
        # same marker mid-sentence is not, which is why this is not the general rule.
        if _LIST_ITEM.match(piece) and pending:
            consumed.add(pending[0])
            out.append((ref, f"{pending[2]} {piece}", start, index))
            continue

        # Only a stem that states a DUTY is worth carrying onto the items. A circular
        # is full of colons that introduce quotations rather than obligations —
        # "the policy states below:" — and in a marked-up draft those sit closer to the
        # list than the real stem does. "shall cover at least the following aspects:"
        # is the sentence that makes each item testable; the other is not.
        if (_LEAD_IN.search(piece) and len(piece) <= _MAX_LEAD_IN_CHARS
                and _OBLIGATION.search(piece)):
            stems.append((index, start, piece))
        out.append((ref, piece, start, index))

    # A stem whose list was found is now carried by every item; on its own it says
    # nothing testable, so it must not reach the reviewer as a clause of its own.
    return [(ref, piece, start) for ref, piece, start, index in out
            if index not in consumed]


def split_clauses(text: str) -> list[dict]:
    """Break the text into clause-sized pieces, keeping character offsets."""
    clauses, collected, cursor = [], [], 0
    blocks = re.split(r"\n\s*\n", text)

    for block in blocks:
        block_start = text.find(block, cursor)
        if block_start == -1:
            block_start = cursor
        cursor = block_start + len(block)

        block = block.strip()
        # A short block is normally a heading — except when it ends in a colon, which
        # makes it the stem of a list that may not start until the next page.
        if len(block) < config.MIN_CLAUSE_CHARS and not _LEAD_IN.search(block):
            continue

        # split a long block on numbered openers so each obligation stands alone
        lines, current, current_ref = block.split("\n"), [], None
        pieces = []
        for line in lines:
            sheet_row = _SHEET_ROW.match(line.strip())
            if sheet_row:
                if current:
                    pieces.append((current_ref, " ".join(current).strip()))
                current_ref = _row_ref(sheet_row.group(1), sheet_row.group(2))
                current = [sheet_row.group(2).strip()]
                continue

            match = _NUMBERED.match(line)
            if match and current:
                pieces.append((current_ref, " ".join(current).strip()))
                current, current_ref = [], match.group(1).strip(".) ")
            elif match:
                current_ref = match.group(1).strip(".) ")
            current.append(line.strip())
        if current:
            pieces.append((current_ref, " ".join(current).strip()))

        for ref, piece in pieces:
            offset = text.find(piece[:60], block_start)
            collected.append((ref, piece, offset if offset != -1 else block_start))

    # Lists are joined to the sentence that introduces them before anything is measured
    # — an item is often short on its own and only reaches a sensible length once it
    # carries its stem.
    for ref, piece, start in _attach_lead_ins(collected):
        if len(piece) < config.MIN_CLAUSE_CHARS:
            continue
        clauses.append({
            "clause_ref": ref,
            "text": piece,
            "char_start": start,
            "char_end": start + len(piece),
        })

    return clauses


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


def _strata_for(text: str) -> str:
    lowered = text.lower()
    for strata, pattern in _STRATA_HINTS:
        if re.search(pattern, lowered):
            return strata
    return "Account Operation"


def is_supersession(text: str) -> bool:
    return bool(_SUPERSESSION.search(text))


def segment_document(conn, document_id: int, text: str) -> int:
    """Store the clauses of one document. Returns how many were kept."""
    found = split_clauses(text)[: config.MAX_CLAUSES_PER_DOC]

    # A short circular — a one-page RIA instruction, an OCR'd image, a spreadsheet row —
    # can fall under the paragraph threshold and yield nothing at all. Losing a whole
    # document to a length rule is worse than keeping a short clause, so fall back to
    # splitting on lines that carry obligation wording.
    if not found and text.strip():
        lines = [ln.strip() for ln in text.split(chr(10)) if len(ln.strip()) > 25]
        cursor = 0
        for line in lines[: config.MAX_CLAUSES_PER_DOC]:
            start = text.find(line, cursor)
            cursor = (start if start != -1 else cursor) + len(line)
            found.append({"clause_ref": None, "text": line,
                          "char_start": max(start, 0),
                          "char_end": max(start, 0) + len(line)})
    approx_chars_per_page = max(len(text) // max(_pages(conn, document_id), 1), 1)

    for sequence, clause in enumerate(found, start=1):
        actionable, strata, reason = classify(clause["text"])
        store.insert(conn, "clauses", {
            "document_id": document_id,
            "clause_ref": clause["clause_ref"] or f"para {sequence}",
            "sequence": sequence,
            "text": clause["text"][:4000],
            "page_number": clause["char_start"] // approx_chars_per_page + 1,
            "char_start": clause["char_start"],
            "char_end": clause["char_end"],
            "is_actionable": 1 if actionable else 0,
            "strata_tag": strata,
            "reason": reason,
        })
    return len(found)


def _pages(conn, document_id: int) -> int:
    row = conn.execute("SELECT pages FROM documents WHERE id = ?", (document_id,)).fetchone()
    return (row["pages"] or 1) if row else 1
