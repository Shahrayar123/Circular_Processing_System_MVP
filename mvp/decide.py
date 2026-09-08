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
from datetime import date, datetime

from . import config, segment, store

# How close a candidate must be for the clause to count as "already covered".
# These are cosine similarities, NOT the fused score. Reciprocal Rank Fusion produces
# roughly the same value for every top-ranked hit (~0.033) because it scores rank, not
# relevance — thresholding on it makes every clause look like a match.
AMEND_THRESHOLD = 0.45
NEW_THRESHOLD = 0.25


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


# ====== THE STUB ENGINE ======

def _decide_rules(clause: dict, candidates: list[dict]) -> dict:
    """Decide from the candidate scores and the clause wording — no model involved.

    Named "rules" rather than "stub" because it is not a placeholder: it produces every
    proposal in a default run, and it is what the model engine falls back to when a call
    fails. A ladder, in order — supersession, then the cosine thresholds.
    """
    text = clause["text"]
    best = candidates[0] if candidates else None
    score = best["score_dense"] if best else 0.0

    if segment.is_supersession(text):
        return {
            "change_type": "Deletion",
            "amendment_type": None,
            "target_test_code": best["test_code"] if best else None,
            "existing_test_description": best["test_description"] if best else None,
            "proposed_test_description": None,
            "exception_code": best["exception_code"] if best else None,
            "proposed_exception_description": None,
            "rationale": ("The clause withdraws or supersedes earlier instructions, so tests "
                          "sourced from the superseded instruction no longer apply. "
                          "Confirm the affected tests before approving."),
            "confidence": 0.72,
        }

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
    data = json.loads(match.group(0)) if match else {}

    proposed = data.get("proposed_test_description")
    best = candidates[0] if candidates else None
    return {
        "change_type": data.get("change_type", "No action"),
        "amendment_type": "Amendment in Test" if data.get("change_type") == "Amendment" else None,
        "target_test_code": data.get("target_test_code"),
        "existing_test_description": best["test_description"] if best else None,
        "proposed_test_description": proposed,
        "exception_code": best["exception_code"] if best else None,
        "proposed_exception_description": _exception_wording(proposed) if proposed else None,
        "rationale": data.get("rationale", ""),
        "confidence": float(data.get("confidence") or 0.6),
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


def _reject_invented_code(decision: dict, valid: set) -> bool:
    """Blank out a test code that is not in the library. True if one was rejected.

    THE GROUNDING RULE. A code the model invented is a fabricated audit control, and it is
    not shown to a reviewer under any circumstances. An Amendment or Deletion that loses
    its target has nothing left to act on, so it becomes No action.
    """
    code = decision.get("target_test_code")
    if not code or code in valid:
        return False

    decision["target_test_code"] = None
    decision["existing_test_description"] = None
    if decision["change_type"] in ("Amendment", "Deletion"):
        decision["change_type"] = "No action"
        decision["rationale"] = (
            "The matched test code could not be validated against the library, "
            "so no change is proposed.")
    return True


def _proposal_row(clause: dict, candidates: list[dict], decision: dict,
                  position: int) -> dict:
    """Assemble the proposal row: the decision, its provenance, and the frozen BEFORE."""
    best = candidates[0] if candidates else None

    # Only snapshot a test the proposal actually ACTS ON. A "New" proposal has a nearest
    # neighbour, but it is not being amended, and recording it as the "before" would imply
    # a change to a test nobody is touching.
    matched = (best if best and decision["target_test_code"] == best["test_code"] else {})

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
        3. reject any test code that is not in the library
        4. store the proposal, with the shortlist and a frozen snapshot of the "before"
    """
    engine_name = engine or config.DECISION_ENGINE
    decide = ENGINES.get(engine_name, _decide_rules)
    valid = _valid_codes()

    obligations = _pending_obligations()
    summary = {"proposals": 0, "dropped_invalid_code": 0, "by_type": {}}

    # Sr numbers continue from what is already there. Restarting at 1 would hand this
    # week's first proposal the same reference as last week's, and the reference is what
    # the audit team quotes in email.
    already = store.scalar("SELECT COUNT(*) FROM proposals") or 0

    with store.connect() as conn:
        for position, clause in enumerate(obligations, start=already + 1):
            candidates = index.search(clause["query"])
            decision = _run_engine(decide, clause, candidates)

            if _reject_invented_code(decision, valid):
                summary["dropped_invalid_code"] += 1

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
