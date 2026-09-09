"""Propose a change for each actionable clause.

Two engines behind one interface:

- **rules** (default) — thresholds over the retrieval scores. Instant, needs no model,
  and always produces the same output, which is what a demo — and a reproducible
  measurement — needs. It is a real engine, not a placeholder.
- **ollama** — a real local model, if one happens to be running. Same output shape.

Either way the rule that matters is preserved: **every proposed test code is checked
against the library before it is stored.** An invented code is dropped. That is the
grounding guarantee of the real system, and it is not a shortcut we take here.
"""

import json
import re
import time
from datetime import date, datetime

from . import config, segment, store

# How close a candidate must be for the clause to count as "already covered".
# These are cosine similarities, NOT the fused score. Reciprocal Rank Fusion produces
# roughly the same value for every top-ranked hit (~0.033) because it scores rank, not
# relevance — thresholding on it makes every clause look like a match.
AMEND_THRESHOLD = 0.45
NEW_THRESHOLD = 0.25

# The only change types this system may store. A value outside this set is not a
# different opinion — it is corruption. `store.is_change()` counts anything that is not
# exactly "No action" as a live change, so a model replying "amendment" or "AMEND" puts a
# row in front of an approver that nothing downstream recognises: ABL's Excel dropdown has
# no matching entry, and the grounding gate below stops recognising it as a change that
# needs a target. Normalised once, on the way in.
CHANGE_TYPES = ("New", "Amendment", "Deletion", "No action")

# How a withdrawal names the instruction it withdraws — "Circular Letter No. 08 of 2021",
# "BPRD Circular No. 11 of 2024". Anchored on "Circular" because that is the only form SBP
# use for something a later circular can withdraw.
#
# Case-insensitive on the reference itself: a scanned circular comes back from OCR in
# whatever case the page had — "CIRCULAR LETTER NO. 08 OF 2021" in a heading, lower case in
# a footnote — and matching only the tidy form loses the reference on exactly the documents
# that needed OCR, which are the ones a human is least likely to re-read carefully.
#
# The issuing-department prefix stays case-SENSITIVE and all-caps via `(?-i:...)`, minus a
# handful of English words. Under a blanket IGNORECASE the prefix matched ordinary words,
# so "instructions contained in Circular Letter No. 09 of 2024" was captured with "in" glued
# to the front and then matched nothing in the library; all-caps OCR did the same with "IN".
_CITED_REFERENCE = re.compile(
    r"((?-i:(?!(?:IN|OF|TO|BY|THE|AND|FOR|VIDE|NO)\b)[A-Z&]{2,10})\s+)?"
    r"(Circular(?:\s+Letter)?\s*No\.?\s*\d+\s+of\s+\d{4})",
    re.IGNORECASE)


# ====== WORDING ======

# Which audit function owns a subject area. In the delivered system a test can belong
# to several departments; the demo routes to the most likely one.
_DEPARTMENT_BY_STRATA = {
    "Internet Banking": "IS&CA",
    "Data & Reporting": "IS&CA",
    "Compliance": "MA",
    "Fraud Prevention": "BA",
    "Branchless Banking": "BA",
    "Account Opening": "BA",
    "Account Operation": "BA",
    "Cash & Teller": "BA",
    "Clearing": "BA",
    "Collection": "BA",
    "Remittances": "BA",
    "Credit": "RR",
    "ATM": "BA",
    "Branch Records": "BA",
    "Income & Expenditure": "MA",
}

# Used to pick out the part of a table row that actually carries the obligation.
_OBLIGATION_WORD = re.compile(
    r"\b(shall|must|required to|advised|should|will be)\b", re.IGNORECASE)


def _lower_first(text: str) -> str:
    """Lower-case the opening word unless it is an acronym or proper noun.

    "AFIs shall develop…" must not become "afis shall develop…". A word carrying any
    capital after its first letter is left alone.
    """
    if not text:
        return text
    first = text.split(" ", 1)[0]
    if len(first) > 1 and any(c.isupper() for c in first[1:]):
        return text
    return text[0].lower() + text[1:]


def _proposed_wording(clause_text: str) -> str:
    """Draft a 'Check that ...' test from the clause, in ABL's house style."""
    text = clause_text.strip()

    # DOCX tables read as "cell | cell | cell", so a heading fragment often arrives on
    # the front of a clause and makes the drafted test read as nonsense. Keep the part
    # that carries the obligation.
    if "|" in text:
        parts = [s.strip() for s in text.split("|") if s.strip()]
        carrying = [s for s in parts if _OBLIGATION_WORD.search(s)]
        text = carrying[0] if carrying else max(parts, key=len)

    # Strip the clause number. Must handle "2.1 " as a whole — an earlier version
    # matched only "2." and left a stray "1" at the front of every proposed test.
    text = re.sub(r"^\s*\(?(?:\d+(?:\.\d+)*|[ivxIVX]{1,5}|[a-z])\)?[.):]?\s+", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    subject = re.split(r"\b(shall|must|are required to|is required to|are advised)\b",
                       text, maxsplit=1, flags=re.IGNORECASE)
    if len(subject) >= 3 and subject[0].strip():
        who = subject[0].strip().rstrip(",")
        duty = subject[2].strip()
        # "X shall be duly filled" -> "X is duly filled", not "X duly filled". Dropping
        # the verb entirely leaves the test ungrammatical.
        verb = "are" if re.search(r"(s|records|forms|documents)$", who.rstrip(".,"),
                                  re.IGNORECASE) else "is"
        duty = re.sub(r"^(be|been)\s+", f"{verb} ", duty)
        sentence = f"Check that {_lower_first(who)} {duty}"
    else:
        sentence = f"Check that {_lower_first(text)}"

    sentence = sentence.split(". ")[0].strip().rstrip(".")

    # A fragment like "Check that account opening form i" helps nobody. Fall back to the
    # fuller clause rather than emit something unusable.
    if len(sentence) < 45:
        fuller = re.sub(r"\s+", " ", clause_text.strip().replace("|", " ")).strip()
        fuller = re.sub(r"^\s*\(?[\divxIVX]+[.)]\s*", "", fuller)
        sentence = f"Check that {_lower_first(fuller)}".rstrip(".")

    if len(sentence) > 240:
        sentence = sentence[:237].rsplit(" ", 1)[0] + "…"
    return sentence + "."


def _exception_wording(proposed: str) -> str:
    body = proposed[len("Check that "):].rstrip(".") if proposed.startswith("Check that ") else proposed
    return f"{body[0].upper()}{body[1:]} — not complied with."


def _root_cause(clause_text: str) -> str:
    lowered = clause_text.lower()
    if re.search(r"system|automat|configur|disabled by the system", lowered):
        return "System Gap"
    if re.search(r"polic(y|ies)|framework|regulation", lowered):
        return "Policy Gap"
    if re.search(r"register|record|maintain|document", lowered):
        return "Process / Procedure Gap"
    return "Implementation Gap"


def _risk(clause_text: str) -> str:
    lowered = clause_text.lower()
    if re.search(r"fraud|forged|sanction|money launder|penal|prohibit", lowered):
        return "High"
    if re.search(r"shall not|must not|immediately|without delay", lowered):
        return "High"
    return "Medium"


# ====== GROUNDING HELPERS ======

def _best_by_cosine(candidates: list[dict]) -> dict | None:
    """The candidate with the highest cosine — NOT candidates[0].

    The list arrives in Reciprocal Rank Fusion order, and RRF scores RANK, not relevance:
    it will place a test with cosine 0.376 above one with cosine 0.379 because BM25 liked
    the first one better. Every threshold in this file is a cosine, so the row being
    thresholded has to be the row with the best cosine — otherwise the engine tests one
    candidate and then proposes a different one. Measured on the demo circular: RRF rank 1
    scored 0.376 while rank 3 scored 0.379.
    """
    return max(candidates, key=lambda c: c["score_dense"]) if candidates else None


def _candidate_by_code(candidates: list[dict], code) -> dict | None:
    """The candidate an engine named, or None if it named one it was never shown.

    Everything a proposal says about the test it acts on — the wording, the exception
    code, the frozen "before" — is read from the row returned here. Reading the code from
    the engine and the wording from `candidates[0]` produced proposals where the two
    belonged to different tests, and on a Deletion that asks an approver to remove one
    control while showing them the text of another.
    """
    wanted = str(code or "").strip().upper()
    for candidate in candidates:
        if str(candidate["test_code"]).upper() == wanted:
            return candidate
    return None


def _normalise_change_type(value) -> str:
    """Map whatever an engine said onto one of CHANGE_TYPES.

    Case and separators are forgiven: "AMENDMENT" and "no_action" are unambiguous. Any
    other word is NOT guessed at — it becomes "No action", because a change type nothing
    downstream understands is more dangerous than no change at all.
    """
    text = re.sub(r"[^a-z ]", " ", str(value or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    for canonical in CHANGE_TYPES:
        if text == canonical.lower():
            return canonical
    return "No action"


def _safe_confidence(value) -> float:
    """A number in 0..1, whatever the engine sent.

    `float("high")` raises, and it raised AFTER the retry loop had spent every attempt —
    so one non-numeric confidence threw a whole obligation onto the fallback engine over a
    field that changes no decision. Coerced and clamped instead.
    """
    try:
        return round(min(1.0, max(0.0, float(value))), 2)
    except (TypeError, ValueError):
        return 0.6


def _same_wording(left, right) -> bool:
    """True when two test descriptions differ only in case, spacing or punctuation."""
    def normalise(text):
        return re.sub(r"[^a-z0-9 ]", "",
                      re.sub(r"\s+", " ", str(text or "").lower())).strip()
    return bool(normalise(left)) and normalise(left) == normalise(right)


# ====== SUPERSESSION — WHICH TESTS A WITHDRAWAL ACTUALLY AFFECTS ======

def _normalise_reference(reference: str) -> str:
    """A comparable form of a circular reference.

    "Circular Letter No. 08 of 2021" and "circular letter no.8 of 2021" name one
    instruction. Compared raw they never match, and every withdrawal then falls back to
    guessing from similarity.
    """
    text = re.sub(r"[^a-z0-9 ]", " ", (reference or "").lower())
    text = re.sub(r"\bno\s*0*(\d+)", r"no \1", text)
    return re.sub(r"\s+", " ", text).strip()


def _cited_references(text: str) -> list[str]:
    """Every instruction a withdrawal clause names, in the order it names them.

    ALL of them, not the first. A withdrawal frequently cites the circular doing the
    withdrawing before the one being withdrawn — "In terms of BPRD Circular No. 44 of
    2026, instructions contained in Circular Letter No. 08 of 2021 shall stand withdrawn"
    — and taking the first match there targets the wrong instruction entirely, which on a
    deletion means proposing to remove the tests that were just created.
    """
    return [f"{(m.group(1) or '').strip()} {m.group(2)}".strip()
            for m in _CITED_REFERENCE.finditer(text or "")]


def _tests_citing(reference: str) -> list[dict]:
    """Every live test whose source_reference is the given instruction.

    THIS is what makes a deletion grounded. A withdrawal says which instruction it
    withdraws; the tests to remove are the ones that CITE it, not the ones that read
    similarly. Similarity answers "which tests are about the same subject", which is a
    different question and on a deletion the wrong one — the nearest test by wording is
    usually still perfectly in force.
    """
    wanted = _normalise_reference(reference)
    if not wanted:
        return []
    rows = store.query("SELECT test_code, test_description, exception_code, "
                       "source_reference FROM audit_tests WHERE is_active = 1 "
                       "ORDER BY test_code")
    return [r for r in rows if _normalise_reference(r["source_reference"]) == wanted]


def _deletion(target: dict | None, confidence: float, rationale: str) -> dict:
    """A Deletion decision built from one library row, or from nothing."""
    return {
        "change_type": "Deletion",
        "amendment_type": None,
        "target_test_code": target["test_code"] if target else None,
        "existing_test_description": target["test_description"] if target else None,
        "proposed_test_description": None,
        "exception_code": target["exception_code"] if target else None,
        "proposed_exception_description": None,
        "rationale": rationale,
        "confidence": confidence,
    }


def _supersession_decision(text: str, candidates: list[dict]) -> dict:
    """A Deletion targeted at the tests the withdrawal actually affects.

    Deletions are the highest-consequence change this system makes, so the target is
    grounded three ways in descending order of certainty, and BOTH the confidence and the
    rationale say which of the three happened. A reviewer must never have to work out for
    themselves whether a deletion target was proved or inferred.

        source match   the clause names an instruction and tests cite it   0.85
        subject only   it names one, but nothing in the library cites it   0.30
        unnamed        it withdraws something without saying what          0.45

    The deletion is raised in all three cases. A missed withdrawal is the most damaging
    error this system can make, and nothing here is ever auto-approved (non-negotiable 6).
    """
    # The right reference is the one the LIBRARY knows about. Trying each in turn resolves
    # the "cites two circulars" case without needing to parse which is which: the circular
    # doing the withdrawing is new, so no test cites it yet.
    references = _cited_references(text)
    reference, cited = (references[0] if references else None), []
    for candidate_reference in references:
        found = _tests_citing(candidate_reference)
        if found:
            reference, cited = candidate_reference, found
            break

    if cited:
        target, others = cited[0], len(cited) - 1
        also = f" {others} further test(s) cite it and need the same decision." if others else ""
        return _deletion(target, 0.85,
                         f"The clause withdraws {reference}. {target['test_code']} is sourced "
                         f"from that instruction, so it no longer has a basis.{also}")

    if reference:
        return _deletion(_best_by_cosine(candidates), 0.30,
                         f"The clause withdraws {reference}, but no test in the library "
                         f"cites that instruction. The nearest test BY SUBJECT is shown as "
                         f"a starting point and is NOT confirmed as affected — identify the "
                         f"affected tests before approving.")

    return _deletion(_best_by_cosine(candidates), 0.45,
                     "The clause withdraws or supersedes earlier instructions but does not "
                     "name them, so the affected tests cannot be identified automatically. "
                     "Confirm which tests came from the withdrawn instruction before "
                     "approving.")


# ====== THE RULES ENGINE ======

def _decide_rules(clause: dict, candidates: list[dict]) -> dict:
    """Decide from the candidate scores and the clause wording — no model involved.

    Named "rules" rather than "stub" because it is not a placeholder: it produces every
    proposal in a default run, and it is what the model engine falls back to when a call
    fails. A ladder, in order — supersession, then the cosine thresholds.
    """
    text = clause["text"]
    # The row being thresholded must be the row with the best cosine — see _best_by_cosine.
    best = _best_by_cosine(candidates)
    score = best["score_dense"] if best else 0.0

    if segment.is_supersession(text):
        return _supersession_decision(text, candidates)

    if best and score >= AMEND_THRESHOLD:
        proposed = _proposed_wording(text)
        return {
            "change_type": "Amendment",
            "amendment_type": "Amendment in Test",
            "target_test_code": best["test_code"],
            "existing_test_description": best["test_description"],
            "proposed_test_description": proposed,
            "exception_code": best["exception_code"],
            "proposed_exception_description": _exception_wording(proposed),
            "rationale": (f"An existing test ({best['test_code']}) already covers this area. "
                          "The clause changes the requirement, so the wording is amended "
                          "rather than a new test being created."),
            "confidence": round(min(0.92, 0.45 + score * 0.6), 2),
        }

    if score >= NEW_THRESHOLD or not candidates:
        proposed = _proposed_wording(text)
        return {
            "change_type": "New",
            "amendment_type": None,
            "target_test_code": None,
            "existing_test_description": None,
            "proposed_test_description": proposed,
            "exception_code": None,
            "proposed_exception_description": _exception_wording(proposed),
            "rationale": ("No existing test covers this obligation closely enough, so a new "
                          "test is proposed."),
            "confidence": 0.66,
        }

    return {
        "change_type": "No action",
        "amendment_type": None,
        "target_test_code": None,
        "existing_test_description": None,
        "proposed_test_description": None,
        "exception_code": None,
        "proposed_exception_description": None,
        "rationale": "The clause creates no testable obligation for the audit checklist.",
        "confidence": 0.60,
    }


# ====== OPTIONAL: A REAL LOCAL MODEL ======

def _decide_ollama(clause: dict, candidates: list[dict]) -> dict:
    import httpx

    listing = "\n".join(
        f"- {c['test_code']}: {c['test_description']}" for c in candidates[:6]) or "- none"
    prompt = (
        "You are helping a bank's internal audit team maintain its audit checklist.\n\n"
        f"CLAUSE:\n{clause['text'][:1500]}\n\nCANDIDATE EXISTING TESTS:\n{listing}\n\n"
        "Reply with one JSON object only: {\"change_type\": \"New|Amendment|Deletion|No action\", "
        "\"target_test_code\": \"code or null\", \"proposed_test_description\": "
        "\"Check that ... or null\", \"rationale\": \"one or two sentences\", "
        "\"confidence\": 0.0-1.0}"
    )
    # Retried up to config.LLM_ATTEMPTS times. A reply that is not usable JSON counts as
    # a failure just like a timeout does: with a 7B model both are usually a bad sample
    # rather than a bad prompt, so asking again on identical input often works. When every
    # attempt is spent the exception propagates to _run_engine, which falls back to the
    # rules engine and says so in the rationale.
    data, last = {}, None
    for attempt in range(1, config.LLM_ATTEMPTS + 1):
        try:
            response = httpx.post(
                f"{config.OLLAMA_URL}/api/chat",
                json={"model": config.OLLAMA_MODEL, "stream": False,
                      "messages": [{"role": "user", "content": prompt}],
                      "options": {"temperature": 0.1}},
                timeout=180,
            )
            response.raise_for_status()
            content = response.json()["message"]["content"]
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                raise ValueError("no JSON object in the reply")
            data = json.loads(match.group(0))
            if attempt > 1:
                print(f"   model call succeeded on attempt {attempt}")
            break
        except Exception as exc:
            last = exc
            if attempt < config.LLM_ATTEMPTS:
                wait = config.LLM_RETRY_BACKOFF * (2 ** (attempt - 1))
                print(f"   model call failed (attempt {attempt}/{config.LLM_ATTEMPTS}): "
                      f"{type(exc).__name__}: {str(exc)[:70]} — retrying in {wait:.0f}s")
                time.sleep(wait)
            else:
                # Raised, not swallowed: _run_engine catches it, falls back to the rules
                # engine, and records that it happened in the proposal's rationale.
                raise RuntimeError(
                    f"model call failed on all {config.LLM_ATTEMPTS} attempts: {last}")

    change_type = _normalise_change_type(data.get("change_type"))
    proposed = data.get("proposed_test_description") or None
    rationale = str(data.get("rationale") or "")

    # Everything about the matched test comes from THE CANDIDATE THE MODEL NAMED, never
    # from candidates[0]. Taking the code from the model and the wording from the top of
    # the list produced proposals whose code and wording belonged to two different tests —
    # on every row of a three-row circular — and on the Deletion that meant asking an
    # approver to remove AT-ITB-0024 while showing them AT-ITB-0018's control.
    matched = _candidate_by_code(candidates, data.get("target_test_code"))

    # A code the model was never shown is ungrounded even when it exists in the library:
    # the model reasoned over eight candidates, so a ninth code came from recall, not from
    # the evidence in front of it. Dropped here, and the grounding gate turns the
    # Amendment or Deletion that depended on it into No action.
    if data.get("target_test_code") and not matched:
        rationale += (f" (the cited code {data['target_test_code']} was not among the "
                      f"candidates supplied, so it was not used)")

    return {
        "change_type": change_type,
        "amendment_type": "Amendment in Test" if change_type == "Amendment" else None,
        "target_test_code": matched["test_code"] if matched else None,
        "existing_test_description": matched["test_description"] if matched else None,
        "proposed_test_description": proposed,
        "exception_code": matched["exception_code"] if matched else None,
        "proposed_exception_description": _exception_wording(proposed) if proposed else None,
        "rationale": rationale,
        "confidence": _safe_confidence(data.get("confidence")),
    }


ENGINES = {"rules": _decide_rules, "ollama": _decide_ollama}


# ====== VALIDATION AND STORAGE ======

def _valid_codes() -> set[str]:
    return {r["test_code"] for r in store.query("SELECT test_code FROM audit_tests")}



def _sr_no(index: int, strata: str) -> str:
    """ABL's identifier shape: year-week-strata-code."""
    today = date.today()
    year, week, _ = today.isocalendar()
    initials = "".join(w[0] for w in re.findall(r"[A-Za-z]+", strata or "GN"))[:2].upper() or "GN"
    return f"{year}-W{week:02d}-{index:02d}-{initials}{index:02d}"


def _pending_obligations() -> list[dict]:
    """Every actionable obligation that does not have a proposal yet.

    ONE PROPOSAL PER OBLIGATION, not per clause. A clause carrying three duties in one
    sentence is three separate audit tests, and searching with the whole sentence returns
    the CENTROID of its meanings — close to none of them. Measured on the demo library, a
    compound clause dropped its best match from 0.700 to 0.504 and pushed another
    obligation's best test from rank 1 to rank 6, outside TOP_K entirely.

    The obligation supplies the QUERY and the proposed wording; the clause supplies the
    source reference, page and offsets. Several proposals therefore share one clause.

    The `NOT IN` is what makes a re-run safe. An earlier version cleared the whole
    proposals table first, which destroyed proposals a reviewer had edited and an approver
    had signed, leaving `approvals` and `proposal_versions` rows pointing at ids that no
    longer existed. A proposal is written ONCE per obligation and thereafter belongs to
    the humans; to re-decide one, delete it deliberately or rebuild with --reset.
    """
    return store.query(
        "SELECT o.id AS obligation_id, o.sequence AS obligation_index, o.text AS query, "
        "       o.strata_tag AS obligation_strata, "
        "       c.*, d.title AS doc_title, d.filename AS doc_filename "
        "FROM obligations o "
        "JOIN clauses c ON c.id = o.clause_id "
        "JOIN documents d ON d.id = c.document_id "
        "WHERE c.is_actionable = 1 "
        "  AND o.id NOT IN (SELECT obligation_id FROM proposals "
        "                   WHERE obligation_id IS NOT NULL) "
        "ORDER BY c.document_id, c.sequence, o.sequence")


def _recover(decision: dict, decide, clause: dict, candidates: list[dict],
             valid: set) -> tuple[dict, bool]:
    """Re-decide with the rules engine when grounding left nothing to review.

    An obligation that has been corrected all the way down to "No action" has been dropped
    from the checklist, and a dropped obligation is invisible — nobody reviews a row that
    is not there. That is the wrong direction to fail in: a spurious proposal is rejected
    in thirty seconds, a missing one is found by the regulator.

    So when the model produces something unusable — an empty amendment, an unrecognised
    change type, a target it never had — the obligation goes back through the deterministic
    engine rather than being abandoned. The rules engine drafts its wording from the clause
    and its target from the scores, so it always has an answer, and it is already the
    fallback for a failed model call (`_run_engine`).

    Returns the decision to store and whether the recovery ran. No recovery when the rules
    engine WAS the engine — re-running it would only produce the same corrected result.
    """
    if decision["change_type"] != "No action" or decide is _decide_rules:
        return decision, False

    retry = _decide_rules(dict(clause, text=clause["query"]), candidates)
    retry = _ground_deletion(retry, clause, candidates)
    if _ground_decision(retry, valid) or retry["change_type"] == "No action":
        # The deterministic engine agrees there is nothing to propose. Keep the original
        # decision, whose rationale explains what the model got wrong.
        return decision, False

    retry["rationale"] += (" (the model's decision could not be grounded, so this was "
                           "decided by the rules engine)")
    return retry, True


def _run_engine(decide, clause: dict, candidates: list[dict]) -> dict:
    """Ask the chosen engine for a decision, falling back to the rules engine on any error.

    Never raises: one bad model call must not stop a weekly batch, and the fallback says
    in the rationale that it happened rather than looking like a normal decision.
    """
    try:
        # The engines draft the proposed test from `text`, so hand them the duty.
        return decide(dict(clause, text=clause["query"]), candidates)
    except Exception as exc:
        decision = _decide_rules(clause, candidates)
        decision["rationale"] += f" (fell back to the rules engine: {exc})"
        return decision


def _ground_decision(decision: dict, valid: set) -> str | None:
    """Make a decision safe to store. Returns the name of what was corrected, or None.

    THE GROUNDING GATE. Every decision from every engine passes through here before it
    reaches a reviewer, and it corrects four things:

        unknown_type    a change type outside CHANGE_TYPES
        invented_code   a code that is not in the library — a fabricated audit control
        missing_target  an Amendment or Deletion with no test to act on
        empty_change    an Amendment whose proposed wording IS the existing wording

    Anything that removes the basis for a change turns the change into "No action" and
    says why in the rationale, where the reviewer can read it. Storing the row with a
    blank field instead would leave someone approving a change whose target nobody can
    name — which is exactly how a fabricated control reaches an audit checklist.

    Ordering matters: the change type is normalised first, because the two checks after it
    ask what kind of change this is.
    """
    if decision["change_type"] not in CHANGE_TYPES:
        _to_no_action(decision, "The engine returned a change type this system does not "
                                "recognise, so no change is proposed.")
        return "unknown_type"

    code = decision.get("target_test_code")

    if code and code not in valid:
        # An Amendment or Deletion that loses its target has nothing left to act on. A New
        # survives: it was never pointing at an existing test, so dropping the stray code
        # costs it nothing.
        if decision["change_type"] in ("Amendment", "Deletion"):
            _to_no_action(decision, "The matched test code could not be validated against "
                                    "the library, so no change is proposed.")
        else:
            decision["target_test_code"] = None
            decision["existing_test_description"] = None
            decision["exception_code"] = None
        return "invented_code"

    if decision["change_type"] in ("Amendment", "Deletion") and not code:
        _to_no_action(decision, f"A {decision['change_type']} needs an existing test to act "
                                f"on, and none was identified, so no change is proposed.")
        return "missing_target"

    if decision["change_type"] == "Amendment" and _same_wording(
            decision.get("proposed_test_description"),
            decision.get("existing_test_description")):
        # A model asked to amend a test will sometimes return that test's own wording back.
        # Stored as-is it is an Amendment that changes nothing: a reviewer reads two
        # identical strings and cannot tell whether the diff is empty or the display broken.
        _to_no_action(decision, f"The proposed wording is identical to {code}'s existing "
                                f"wording, so there is nothing to amend.")
        return "empty_change"

    return None


def _to_no_action(decision: dict, reason: str) -> None:
    """Downgrade a decision to No action, keeping the reason where a reviewer sees it.

    The confidence is capped rather than overwritten: a downgrade is not a confident
    judgement about the clause, and leaving the engine's 0.9 on it would sort a corrected
    row above genuine proposals on the review screen.
    """
    decision["change_type"] = "No action"
    decision["amendment_type"] = None
    decision["target_test_code"] = None
    decision["existing_test_description"] = None
    decision["exception_code"] = None
    decision["proposed_test_description"] = None
    decision["proposed_exception_description"] = None
    decision["rationale"] = reason
    decision["confidence"] = min(_safe_confidence(decision.get("confidence")), 0.5)


def _ground_deletion(decision: dict, clause: dict, candidates: list[dict]) -> dict:
    """Re-target any Deletion through the source-grounded supersession path.

    A withdrawal must not depend on a similarity score OR on the model's opinion of which
    test it hits. Whichever engine raised the deletion — the regex in the rules engine, or
    the model noticing a withdrawal the regex missed — the TARGET is chosen the same
    deterministic way, from the instruction the clause names. That is what stops two
    engines proposing to delete two different controls from one sentence, which is what
    they did before this existed.

    Deletion is the only change type treated this way, because it is the only one where
    the wrong target destroys a control that is still in force.
    """
    if decision["change_type"] != "Deletion":
        return decision
    return _supersession_decision(clause["query"], candidates)


def _proposal_row(clause: dict, candidates: list[dict], decision: dict,
                  position: int) -> dict:
    """Assemble the proposal row: the decision, its provenance, and the frozen BEFORE."""
    best = _best_by_cosine(candidates)

    # Only snapshot a test the proposal actually ACTS ON, and find it BY CODE. A "New"
    # proposal has a nearest neighbour, but it is not being amended, and recording it as
    # the "before" would imply a change to a test nobody is touching.
    #
    # Looked up by code rather than compared against candidates[0], because an engine may
    # legitimately choose any candidate on the shortlist. Comparing against the top one
    # left the whole frozen snapshot — exception code, strata, department, risk, source —
    # silently NULL on every proposal that acted on candidate 2 or lower, which was every
    # proposal the model engine produced.
    matched = _candidate_by_code(candidates, decision["target_test_code"]) or {}

    # An obligation can concern a different strata from its clause as a whole —
    # "verify agents ... and retain records" is branchless banking plus records.
    strata = clause["obligation_strata"] or clause["strata_tag"]

    # Route by the SUBJECT of the clause, not by whichever test happened to rank first.
    # Taking the candidate's department sent internet-banking clauses to Shariah Audit,
    # because department is assigned at random in the demo library.
    department = _DEPARTMENT_BY_STRATA.get(strata, best["department"] if best else "BA")

    return {
        "clause_id": clause["id"],
        "obligation_id": clause["obligation_id"],
        "obligation_index": clause["obligation_index"],
        "document_id": clause["document_id"],
        "sr_no": _sr_no(position, strata),
        "change_type": decision["change_type"],
        "amendment_type": decision["amendment_type"],
        "target_test_code": decision["target_test_code"],
        "existing_test_description": decision["existing_test_description"],
        "proposed_test_description": decision["proposed_test_description"],
        "exception_code": decision["exception_code"],
        "proposed_exception_description": decision["proposed_exception_description"],
        # The BEFORE snapshot — the matched test exactly as the library held it when this
        # decision was taken. Frozen deliberately: the library moves, and a reviewer must
        # see what they actually approved a change against.
        "existing_exception_code": matched.get("exception_code"),
        "existing_exception_description": matched.get("exception_description"),
        "existing_strata": matched.get("strata"),
        "existing_department": matched.get("department"),
        "existing_risk_rating": matched.get("risk_rating"),
        "existing_source_reference": matched.get("source_reference"),
        "decided_at": datetime.now().isoformat(timespec="seconds"),
        "strata": strata or (best["strata"] if best else ""),
        "department": department,
        "risk_rating": _risk(clause["query"]),
        "root_cause": _root_cause(clause["query"]),
        "rationale": decision["rationale"],
        "confidence": decision["confidence"],
        # The shortlist the engine actually considered, so the review screen shows what
        # was weighed at decision time rather than what a fresh search returns today.
        "candidates": json.dumps([
            {"test_code": c["test_code"], "test_description": c["test_description"],
             "strata": c["strata"], "score": c["score_fused"]}
            for c in candidates[:6]]),
    }


def decide_all(index, engine: str = None) -> dict:
    """Propose a change for every obligation that does not have one. Returns a summary.

    For each obligation, in order:
        1. search the library with the DUTY, not the whole clause
        2. ask the engine for a change type and wording
        3. re-target any Deletion from the instruction the clause withdraws
        4. ground the decision — no invented code, no change without a target, no
           Amendment that amends nothing
        5. store the proposal, with the shortlist and a frozen snapshot of the "before"
    """
    engine_name = engine or config.DECISION_ENGINE
    decide = ENGINES.get(engine_name, _decide_rules)
    valid = _valid_codes()

    obligations = _pending_obligations()
    # `corrected` counts what the grounding gate had to fix, by kind. It is printed at the
    # end of the stage: a run that silently corrected forty decisions is telling you
    # something about the engine, and a count nobody prints is a count nobody reads.
    summary = {"proposals": 0, "corrected": {}, "by_type": {}}

    # Sr numbers continue from what is already there. Restarting at 1 would hand this
    # week's first proposal the same reference as last week's, and the reference is what
    # the audit team quotes in email.
    already = store.scalar("SELECT COUNT(*) FROM proposals") or 0

    with store.connect() as conn:
        for position, clause in enumerate(obligations, start=already + 1):
            candidates = index.search(clause["query"])
            decision = _run_engine(decide, clause, candidates)
            decision = _ground_deletion(decision, clause, candidates)

            correction = _ground_decision(decision, valid)
            if correction:
                summary["corrected"][correction] = summary["corrected"].get(correction, 0) + 1
                decision, recovered = _recover(decision, decide, clause, candidates, valid)
                if recovered:
                    summary["corrected"]["recovered_by_rules"] = (
                        summary["corrected"].get("recovered_by_rules", 0) + 1)

            _debug_decision(clause, candidates, decision)

            store.insert(conn, "proposals",
                         _proposal_row(clause, candidates, decision, position))
            summary["proposals"] += 1
            key = decision["change_type"]
            summary["by_type"][key] = summary["by_type"].get(key, 0) + 1

    return summary


# ===== DEBUG PRINTS — delete this function and its one call site when done =====

def _debug_decision(clause: dict, candidates: list[dict], decision: dict) -> None:
    """Print the query, the candidates retrieval returned, and what was decided."""
    print(f"[DECIDE] [{clause.get('clause_ref') or clause.get('id')}] "
          f"obligation {clause.get('obligation_index')}")
    print(f"[DECIDE]   query: " + clause["query"][:150].replace("\n", " "))
    if not candidates:
        print("[DECIDE]   retrieval returned NOTHING — is the index built?")
    for i, c in enumerate(candidates[:5], start=1):
        mark = "<--" if c["test_code"] == decision.get("target_test_code") else "   "
        # cosine is what the thresholds read. rrf only ORDERS the list: RRF scores rank,
        # not relevance, so every top hit is about 0.033.
        print(f"[DECIDE]  {mark}{i}. {c['test_code']} "
              f"cosine={c['score_dense']:.3f} bm25={c['score_bm25']:>6.2f} "
              f"rrf={c['score_fused']:.5f} {c['test_description'][:50]}")
    print(f"[DECIDE]   => {decision['change_type']} "
          f"{decision.get('target_test_code') or ''} "
          f"confidence={decision.get('confidence')}")
