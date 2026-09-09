"""Paths and settings for the MVP.

Everything is relative to the MVP folder, so the demo runs from anywhere.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent

# Load MVP/.env before any os.getenv below reads a default.
#
# The path is resolved from THIS FILE's location, never from the current directory. The
# pipeline is run from the MVP folder, the Streamlit apps from wherever the terminal
# happens to be, and the debugger from the project root — a relative ".env" would load
# nothing in two of those three, silently, and you would be looking at defaults while
# reading a file that says otherwise.
#
# `override=False` so a real environment variable beats the file: on a machine where
# someone has exported MVP_OLLAMA_URL, that is deliberate and the file must not undo it.
load_dotenv(ROOT / ".env", override=False)

CIRCULARS_DIR = ROOT / "Circulars Data"
EMAILS_DIR = ROOT / "RIA Emails"
OUTPUT_DIR = ROOT / "output"
DB_PATH = ROOT / "mvp_demo.db"

# ====== DEMO SIZES ======
# The real system indexes ~7,500 tests. 500 keeps the demo instant.
LIBRARY_SIZE = 500
TOP_K = 8                      # candidates shown to the decision step
MIN_CLAUSE_CHARS = 60          # shorter fragments are headings, not obligations.
                               # Kept low because real circulars are often short —
                               # a one-page RIA instruction must not yield 0 clauses.
                               # A short fragment that STATES A DUTY is kept anyway —
                               # see MIN_OBLIGATION_CLAUSE_CHARS below.

# The floor for a fragment that is short but carries obligation wording. Length is only
# a proxy for "this is a heading"; obligation wording is direct evidence that it is not.
# "Banks shall reconcile ATM cassettes daily." is 42 characters and is a real audit test;
# dropping it on length loses a control with no error anywhere. This floor exists so that
# a stray "shall" in a five-word fragment still cannot become a clause.
MIN_OBLIGATION_CLAUSE_CHARS = 25
MAX_CLAUSES_PER_DOC = 250      # a cap against a runaway document, not a sample size.
                               # It was 40, which silently TRUNCATED a 48-page manual —
                               # once lettered lists are split into their items, a real
                               # circular passes 150 clauses easily and the tail was
                               # being dropped without a word. The run is still ~2s.

# ====== DECISION ENGINE ======
# "rules" is deterministic and instant — always works, no model needed. It is a real
# decision engine, not a placeholder: thresholds over the retrieval scores.
# "ollama" uses a local model if one is running.
DECISION_ENGINE = os.getenv("MVP_ENGINE", "rules")

# Who judges whether a clause creates an obligation: "rules" (keyword patterns, instant,
# always available) or "llm" (a local model reads the clause). The delivered system uses
# the model; the rules stay as the fallback and as the no-install path.
ACTIONABILITY_JUDGE = os.getenv("MVP_JUDGE", "rules")
# How many times a model call is attempted before the rules take over. A 7B model
# intermittently returns a truncated object, an array of the wrong length, or nothing at
# all within the timeout — and asking again usually works, because the failure is a bad
# sample rather than a bad prompt. Three is a compromise: enough to ride out a transient
# fault, few enough that a genuinely broken prompt fails the batch in a minute rather
# than five. Every attempt after the first is REPORTED, never silent.
LLM_ATTEMPTS = int(os.getenv("MVP_LLM_ATTEMPTS", "3"))

# Seconds to wait after a failed attempt, doubling each time (1s, then 2s). Short,
# because the usual cause is a bad sample and not a server that needs time to recover.
LLM_RETRY_BACKOFF = float(os.getenv("MVP_LLM_RETRY_BACKOFF", "1.0"))

OLLAMA_URL = os.getenv("MVP_OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("MVP_OLLAMA_MODEL", "qwen2.5:7b")

# ====== ABL VOCABULARY ======
# Taken from the real working file ABL supplied with their BRD, so the demo output
# uses their words rather than ours.

DEPARTMENTS = ["BA", "MA", "IS&CA", "RR", "SA"]

DEPARTMENT_NAMES = {
    "BA": "Branch Audit",
    "MA": "Management Audit",
    "IS&CA": "Information System & Continuous Auditing",
    "RR": "Risk Review",
    "SA": "Internal Shariah Audit",
}

STRATA = [
    "Account Opening", "Account Operation", "Asaan Accounts", "Sole Proprietor",
    "Pensioner", "Student", "Illiterate", "Housewife / Dependents", "Politician",
    "ABL Staff", "Foreign Currency (Individuals)", "Cash & Teller", "Clearing",
    "Collection", "Credit", "ATM", "Branch Records", "Income & Expenditure",
    "Remittances", "Internet Banking", "Branchless Banking", "Compliance",
    "Fraud Prevention", "Data & Reporting",
]

CHANGE_TYPES = ["New", "Amendment", "Deletion", "No action"]

AMENDMENT_TYPES = [
    "Amendment in Test", "Amendment in Exception", "Amendment in Strata",
    "Amendment in Reference", "Amendment in Risk",
]

RISK_RATINGS = ["High", "Medium", "Low"]

ROOT_CAUSES = ["Policy Gap", "Process / Procedure Gap", "System Gap", "Implementation Gap"]


def describe(judge: str = None, engine: str = None) -> str:
    """One line naming the settings that change what a run DOES. Printed at startup.

    Takes the EFFECTIVE values — the ones the run will actually use — not the module
    constants. A command-line flag overrides .env, so reading the constants here would
    print `judge=llm` on a run launched with `--judge rules` and quietly contradict
    itself. A banner that can disagree with the run is worse than no banner.

    A reader needs the model name too: "judged by the model" means nothing without saying
    which model, and a run in a screenshot six weeks from now has no other way to say.
    """
    judge = judge or ACTIONABILITY_JUDGE
    engine = engine or DECISION_ENGINE
    uses_model = judge == "llm" or engine == "ollama"
    model = f" · model {OLLAMA_MODEL} at {OLLAMA_URL}" if uses_model else ""
    source = ".env" if (ROOT / ".env").exists() else "built-in defaults"
    return f"judge={judge} · engine={engine}{model}  (settings from {source})"


def ensure_dirs() -> None:
    """Create the folders the pipeline writes to. Safe to call repeatedly."""
    for path in (OUTPUT_DIR, EMAILS_DIR):
        path.mkdir(parents=True, exist_ok=True)
