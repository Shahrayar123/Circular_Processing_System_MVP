"""Paths and settings for the MVP.

Everything is relative to the MVP folder, so the demo runs from anywhere.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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
MAX_CLAUSES_PER_DOC = 250      # a cap against a runaway document, not a sample size.
                               # It was 40, which silently TRUNCATED a 48-page manual —
                               # once lettered lists are split into their items, a real
                               # circular passes 150 clauses easily and the tail was
                               # being dropped without a word. The run is still ~2s.

# ====== DECISION ENGINE ======
# "stub" is deterministic and instant — always works, no model needed.
# "ollama" uses a local model if one is running.
DECISION_ENGINE = os.getenv("MVP_ENGINE", "stub")
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


def ensure_dirs() -> None:
    for path in (OUTPUT_DIR, EMAILS_DIR):
        path.mkdir(parents=True, exist_ok=True)
