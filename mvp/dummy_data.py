"""Generates the 500 dummy audit tests.

DEMO DATA. Not ABL's real library — that is 7,500 tests and has not been supplied.
The shape follows the working file ABL sent with their BRD (test code, description,
exception, strata, department, risk rating, source reference) so the demo output looks
like their own file rather than ours.

The topics are deliberately weighted towards the documents ABL supplied — compliance,
fraud, internet banking, branchless banking, data reporting — so the matching step has
something real to find.
"""

import random

from . import config

# ====== TEST CONTENT BY THEME ======
# Each theme provides subjects and checks that get combined into "Check that ..." tests,
# the wording convention ABL uses.

THEMES = {
    "Compliance": {
        "strata": ["Compliance", "Account Opening", "Politician", "ABL Staff"],
        "subjects": [
            "customer due diligence records", "customer risk profiles",
            "politically exposed person screening", "sanctions screening logs",
            "beneficial ownership information", "compliance risk assessments",
            "suspicious transaction reporting", "customer risk ratings",
            "periodic KYC review records", "compliance training registers",
        ],
        "checks": [
            "are maintained and updated as required by the Compliance Policy",
            "are reviewed and approved by the designated compliance officer",
            "are completed within the timeline prescribed in the policy",
            "are supported by adequate documentary evidence",
            "are escalated to Compliance Group where thresholds are breached",
        ],
        "refs": ["Compliance Policy V7.0", "BPRD Circular No. 03 of 2023", "AML/CFT Regulations"],
    },
    "Fraud Prevention": {
        "strata": ["Fraud Prevention", "Cash & Teller", "Clearing", "Account Operation"],
        "subjects": [
            "instruments presented for payment", "call back confirmation records",
            "fraud incident registers", "dormant account activation requests",
            "signature verification records", "cheque security features",
            "fraud awareness training logs", "staff rotation records",
            "unusual transaction alerts", "whistle-blowing registers",
        ],
        "checks": [
            "are verified before the transaction is processed",
            "are recorded in the register maintained for the purpose",
            "are reported to the concerned department without delay",
            "are checked against the prescribed control matrix",
            "are authorised by an officer of appropriate seniority",
        ],
        "refs": ["Fraud Prevention Manual", "P/INST-2025/254", "Circular Letter No. 09 of 2024"],
    },
    "Internet Banking": {
        "strata": ["Internet Banking", "Account Operation", "ATM"],
        "subjects": [
            "multi-factor authentication settings", "user access logs",
            "session timeout configuration", "internet banking enrolment requests",
            "privileged access registers", "transaction limits configured in the system",
            "customer consent records for digital channels", "security patch records",
            "penetration testing reports", "dormant digital user accounts",
        ],
        "checks": [
            "are configured in line with the Internet Banking Security Framework",
            "are reviewed at the frequency prescribed in the framework",
            "are retained for the required retention period",
            "are approved before being applied in the production environment",
            "are monitored and exceptions investigated",
        ],
        "refs": ["Internet Banking Security Framework", "BPRD Circular No. 18 of 2023"],
    },
    "Branchless Banking": {
        "strata": ["Branchless Banking", "Asaan Accounts", "Remittances"],
        "subjects": [
            "branchless banking agent records", "level 0 account opening documents",
            "agent commission payments", "agent due diligence files",
            "transaction limits for BB accounts", "biometric verification records",
            "agent monitoring reports", "customer complaint records for BB channels",
        ],
        "checks": [
            "are maintained in accordance with the Branchless Banking Regulations",
            "are verified before the agent is on-boarded",
            "are within the limits prescribed by the regulator",
            "are subject to periodic monitoring by the concerned function",
            "are reconciled and differences investigated",
        ],
        "refs": ["Branchless Banking Regulations", "BPRD Circular No. 11 of 2024"],
    },
    "Data & Reporting": {
        "strata": ["Data & Reporting", "Income & Expenditure", "Branch Records"],
        "subjects": [
            "regulatory returns submitted to SBP", "data quality exception reports",
            "reconciliation of reported figures", "BI reporting access matrices",
            "data retention records", "reporting calendars",
            "manual adjustments made to reported data", "source system reconciliations",
        ],
        "checks": [
            "are prepared in line with the Data Architecture and BI Reporting manual",
            "are submitted within the prescribed due date",
            "are reviewed and signed off before submission",
            "are supported by an audit trail of changes",
            "are consistent with the underlying source system",
        ],
        "refs": ["Data Architecture & BI Reporting Manual", "Reporting Chart of Accounts"],
    },
    "General Banking": {
        "strata": ["Account Opening", "Pensioner", "Student", "Illiterate",
                   "Housewife / Dependents", "Sole Proprietor", "Credit",
                   "Foreign Currency (Individuals)", "Collection", "Branch Records"],
        "subjects": [
            "account opening forms", "customer signature cards",
            "statement of account dispatch records", "locker agreements",
            "deceased account registers", "term deposit receipts",
            "stop payment instructions", "standing instruction records",
            "account closure registers", "zakat deduction records",
            "profit payment records", "unclaimed deposit registers",
        ],
        "checks": [
            "are complete and duly signed by the authorised person",
            "are held in safe custody and retrievable on request",
            "are updated within the prescribed turnaround time",
            "are checked for accuracy by an independent officer",
            "are supported by the documents prescribed in the manual",
        ],
        "refs": ["Branch Operations Manual", "SBP Prudential Regulations", "P/INST-2025/253"],
    },
}

EXCEPTIONS = [
    "{subject} were found incomplete",
    "{subject} were not available for verification",
    "{subject} were not updated within the prescribed timeline",
    "{subject} were not authorised by the competent authority",
    "{subject} showed discrepancies that were not investigated",
]

# A third axis so the library reaches 500 genuinely distinct descriptions rather than
# repeating the same wording — a library of near-duplicates makes matching look better
# than it is.
SCOPES = [
    "",
    " for the period under review",
    " on a sample basis",
    " at the branch",
    " and exceptions are documented",
]

DEPT_WEIGHTS = [("BA", 0.55), ("MA", 0.18), ("IS&CA", 0.15), ("RR", 0.08), ("SA", 0.04)]


def _weighted_department(rng: random.Random) -> str:
    roll = rng.random()
    running = 0.0
    for code, weight in DEPT_WEIGHTS:
        running += weight
        if roll <= running:
            return code
    return "BA"


def generate(count: int = None, seed: int = 20260831) -> list[dict]:
    """Build `count` distinct audit tests.

    Distinctness matters: a library where the same description repeats makes retrieval
    metrics meaningless. Every test here is a unique subject/check pairing.
    """
    count = count or config.LIBRARY_SIZE
    rng = random.Random(seed)

    pairs = []
    for theme, spec in THEMES.items():
        for subject in spec["subjects"]:
            for check in spec["checks"]:
                for scope in SCOPES:
                    pairs.append((theme, spec, subject, check, scope))
    rng.shuffle(pairs)

    prefix = {"Compliance": "CMP", "Fraud Prevention": "FRD", "Internet Banking": "ITB",
              "Branchless Banking": "BBK", "Data & Reporting": "DAR", "General Banking": "GBK"}

    tests, seen, serial = [], set(), {}
    for theme, spec, subject, check, scope in pairs:
        description = f"Check that {subject} {check}{scope}."
        if description in seen:
            continue
        seen.add(description)

        code_prefix = prefix[theme]
        serial[code_prefix] = serial.get(code_prefix, 0) + 1
        test_code = f"AT-{code_prefix}-{serial[code_prefix]:04d}"

        exception = rng.choice(EXCEPTIONS).format(subject=subject[0].upper() + subject[1:])

        tests.append({
            "test_code": test_code,
            "test_description": description,
            "exception_code": f"EX-{code_prefix}-{serial[code_prefix]:04d}",
            "exception_description": exception,
            "strata": rng.choice(spec["strata"]),
            "department": _weighted_department(rng),
            "risk_rating": rng.choices(config.RISK_RATINGS, weights=[0.3, 0.5, 0.2])[0],
            "source_reference": rng.choice(spec["refs"]),
            "is_active": 1,
        })
        if len(tests) >= count:
            break

    return tests
