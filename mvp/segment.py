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

    `pieces` is [(ref, text, start, end)] in document order; the same shape comes back.
    """
    stems: list[tuple[int, int, str]] = []          # (source index, start, text)
    out: list[tuple] = []                           # (ref, text, start, end, source index)
    consumed: set[int] = set()

    for index, (ref, piece, start, end) in enumerate(pieces):
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
                # Every item of a list shares its parent block's span. They overlap, so
                # anything measuring coverage has to merge spans rather than sum them.
                out.append((ref, f"{stem_text} {item}" if stem_text else item,
                            start, end, index))
            continue

        # A list whose items are split across blocks — a page break falls between "a)"
        # and "b)", so each arrives alone and the two-item rule above never fires. A
        # marker at the very START of a block is a safe enough signal on its own; the
        # same marker mid-sentence is not, which is why this is not the general rule.
        if _LIST_ITEM.match(piece) and pending:
            consumed.add(pending[0])
            out.append((ref, f"{pending[2]} {piece}", start, end, index))
            continue

        # Only a stem that states a DUTY is worth carrying onto the items. A circular
        # is full of colons that introduce quotations rather than obligations —
        # "the policy states below:" — and in a marked-up draft those sit closer to the
        # list than the real stem does. "shall cover at least the following aspects:"
        # is the sentence that makes each item testable; the other is not.
        if (_LEAD_IN.search(piece) and len(piece) <= _MAX_LEAD_IN_CHARS
                and _OBLIGATION.search(piece)):
            stems.append((index, start, piece))
        out.append((ref, piece, start, end, index))

    # A stem whose list was found is now carried by every item; on its own it says
    # nothing testable, so it must not reach the reviewer as a clause of its own.
    return [(ref, piece, start, end) for ref, piece, start, end, index in out
            if index not in consumed]


def split_clauses(text: str) -> list[dict]:
    """Break the text into clause-sized pieces, keeping character offsets."""
    clauses, collected, cursor = [], [], 0
    blocks = re.split(r"\n\s*\n", text)

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

        # A short block is normally a heading — except when it ends in a colon, which
        # makes it the stem of a list that may not start until the next page, or when it
        # states a duty despite its length.
        if (len(block) < config.MIN_CLAUSE_CHARS and not _LEAD_IN.search(block)
                and not _states_a_duty(block)):
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
        pieces = []                       # (ref, text, start, end)
        piece_start = piece_end = block_start
        line_at = block_start             # absolute offset of the current line

        def flush():
            """Close the piece being built, if it has anything in it."""
            if current:
                pieces.append((current_ref, " ".join(current).strip(),
                               piece_start, piece_end))

        for line in lines:
            lead = len(line) - len(line.lstrip())          # indent, kept out of offsets
            content_at = line_at + lead
            content_end = line_at + len(line.rstrip())
            stripped = line.strip()

            sheet_row = _SHEET_ROW.match(stripped)
            if sheet_row:
                flush()
                current_ref = _row_ref(sheet_row.group(1), sheet_row.group(2))
                current = [sheet_row.group(2).strip()]
                # Point past the "[row 4] " marker at the row's actual content.
                piece_start = content_at + sheet_row.start(2)
                piece_end = content_end
                line_at += len(line) + 1
                continue

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

            current.append(stripped)
            piece_end = content_end
            line_at += len(line) + 1       # +1 for the newline split() removed

        flush()
        collected.extend(pieces)

    # Lists are joined to the sentence that introduces them before anything is measured
    # — an item is often short on its own and only reaches a sensible length once it
    # carries its stem.
    for ref, piece, start, end in _attach_lead_ins(collected):
        if len(piece) < config.MIN_CLAUSE_CHARS and not _states_a_duty(piece):
            continue
        clauses.append({
            "clause_ref": ref,
            "text": piece,
            "char_start": start,
            # The measured end of the source span, NOT start + len(piece). A piece is its
            # lines joined with spaces, so its length only approximates the span it came
            # from — and an approximate end makes every coverage figure approximate too.
            "char_end": max(end, start + 1),
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
    parts = [p.strip(" ,;") for p in _OBLIGATION_JOIN.split(text) if p.strip(" ,;")]
    if len(parts) < 2:
        return [text]

    # Carry the subject onto the later duties. "Banks shall verify X, and shall maintain
    # Y" gives "shall maintain Y" — true to the source but useless as a query, because
    # the subject is what the retrieval needs to match a branchless-banking test.
    subject = text.split(" shall")[0].split(" must")[0].strip()
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
    return out[:MAX_OBLIGATIONS_PER_CLAUSE]


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


def _split_with_fallback(text: str) -> tuple[list[dict], str]:
    """Split the text into clauses, and say WHICH rung of the ladder produced them.

    Rung 1 is structure — numbering, headings, table rows, spreadsheet rows — and is what
    almost every document hits. Rung 2 exists because a short circular (a one-page RIA
    instruction, an OCR'd image, a single spreadsheet row) can fall under the paragraph
    threshold and yield nothing at all, and losing a whole document to a length rule is
    worse than keeping a rough clause.

    The rung is returned, not hidden, because a fallback split must never be
    indistinguishable from a structural one.
    """
    found = split_clauses(text)[: config.MAX_CLAUSES_PER_DOC]
    if found or not text.strip():
        return found, "structure"

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
    return found, "obligation-lines"


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
    """Write one clause row and return its id."""
    actionable, strata, reason, judged_by = verdict
    return store.insert(conn, "clauses", {
        "document_id": document_id,
        "clause_ref": clause["clause_ref"] or f"para {sequence}",
        "sequence": sequence,
        # Stored WHOLE. Only what is sent to a model is capped, and that is recorded on
        # the row rather than done quietly.
        "text": clause["text"],
        "page_number": clause["char_start"] // chars_per_page + 1,
        "char_start": clause["char_start"],
        "char_end": clause["char_end"],
        "is_actionable": 1 if actionable else 0,
        "strata_tag": strata,
        "reason": reason,
        "judged_by": judged_by,
        "segmented_by": rung,
        "model_input_truncated": 1 if len(clause["text"]) > JUDGE_INPUT_CHARS else 0,
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


def segment_document(conn, document_id: int, text: str, judge: str = "rules") -> dict:
    """Split one document into clauses, judge each, and store them.

    Four steps, in order:
        1. split          -> clauses, and which rung of the ladder produced them
        2. judge          -> is this clause an obligation the bank can be audited against
        3. sanity-check   -> flag a split that looks wrong; never fail the document for it
        4. store          -> one clause row each, plus its obligations if it is actionable

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

    found, rung = _split_with_fallback(text)
    verdicts = _judge_clauses(found, judge)

    # A split that looks wrong FLAGS the document; it never fails it. Someone has to
    # notice, and a low clause count three screens later is not noticing.
    warning = check_split(found, text)
    if warning:
        conn.execute("UPDATE documents SET segment_warning = ? WHERE id = ?",
                     (warning, document_id))

    _debug_split(document_id, text, found, verdicts, rung, warning)

    # Page numbers are estimated from character position: the readers give us text, not
    # a page-to-character map. Good enough to send a reviewer to the right page.
    chars_per_page = max(len(text) // max(_pages(conn, document_id), 1), 1)

    for sequence, (clause, verdict) in enumerate(zip(found, verdicts), start=1):
        clause_id = _store_clause(conn, document_id, sequence, clause, verdict,
                                  rung, chars_per_page)
        actionable, strata = verdict[0], verdict[1]
        # Only an actionable clause has duties worth pulling apart. A heading with a
        # comma in it is not three obligations.
        if actionable:
            duties = _store_obligations(conn, clause_id, clause["text"], strata)
            _debug_obligations(clause["clause_ref"] or f"para {sequence}",
                               clause["text"], duties)
    return {"clauses": len(found),
            "actionable": sum(1 for v in verdicts if v[0]),
            "warning": warning}


# ===== DEBUG PRINTS — delete both functions and their two call sites when done =====

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
