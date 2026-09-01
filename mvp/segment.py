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


def split_clauses(text: str) -> list[dict]:
    """Break the text into clause-sized pieces, keeping character offsets."""
    clauses, cursor = [], 0
    blocks = re.split(r"\n\s*\n", text)

    for block in blocks:
        block_start = text.find(block, cursor)
        if block_start == -1:
            block_start = cursor
        cursor = block_start + len(block)

        block = block.strip()
        if len(block) < config.MIN_CLAUSE_CHARS:
            continue

        # split a long block on numbered openers so each obligation stands alone
        lines, current, current_ref = block.split("\n"), [], None
        pieces = []
        for line in lines:
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
            if len(piece) < config.MIN_CLAUSE_CHARS:
                continue
            offset = text.find(piece[:60], block_start)
            start = offset if offset != -1 else block_start
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
