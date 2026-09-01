# AuditPilot MVP

A working demonstration of the audit-checklist process, running on ABL's own documents.
Built to show what the system does and what it produces — **not** the delivered system.

The real build is prompted in `../For Live Work/` and is untouched by anything here.

---

## Run it

Two commands. No model downloads, no database server, no GPU.

```bash
python run_pipeline.py
```

```bash
streamlit run app.py
```

There are **two screens**, sharing one database. Approve something on either and the
other shows it approved.

| | For | What it is |
|---|---|---|
| `streamlit run app.py` | walking through the whole system | six tabs — dashboard, documents, review, approvals, exports, solution map |
| `streamlit run simple_app.py` | **the simple client demo** | one screen: the table of proposed changes → reviewer sign-off → approver sign-off → Excel export |

`simple_app.py` is the one to open when the point of the meeting is the **workflow**
rather than the system. It is a table and two sign-offs, nothing else — but the rules
underneath it are the real ones: level 1 only advances a change, only level 2 releases
it, deletions need an explicit tick, and the export carries approved rows only.

The pipeline takes about 12 seconds. It prints each stage as it goes, then writes the
Word and Excel outputs to `output/`.

---

## What it does

The process flow is the one in the functional document —
**circular intake → AI analysis → Excel working file → human review → eAudit hand-off**:

```
Circulars Data/  +  RIA Emails/
        ↓  intake, de-duplicate by SHA-256
   documents  ──────────────────────────────  3 duplicates detected and skipped
        ↓  read (PDF / DOCX / email body)
   clause splitting  →  actionable or "for information only"
        ↓
   search 500 audit tests  (keyword + vector, fused)
        ↓
   propose  New / Amendment / Deletion / No action  + rationale
        ↓  every cited test code validated against the library
   Word working document  +  Excel working file
        ↓
   REVIEW  (level 1)  →  APPROVAL  (level 2)
        ↓  approved changes only
   eAudit BAC export
```

The six tabs in the app follow that flow: **Dashboard · Documents · Review ·
Approvals · Exports · Solution map**. The last one maps each component of the agreed
solution onto what the demo does, and says where a shortcut was taken.

**Review and Approvals are one-at-a-time queues.** It shows a single proposal, in the same clause
order as the Documents tab, with a "Reviewing 3 of 10" counter. Decide it — approve,
reject or request changes — and the next one appears. "Skip for now" moves past without
deciding; skipped items come back once the rest are done. Switch the *Show* control to
**All proposals** to see everything again, including what you have already decided.
"Request changes" is the exception — it stays in the queue, because it needs reworking.

**Approvals works the same way** for level 2. A table lists everything waiting, in the
same clause order, with two columns showing **what the reviewer decided** — their note
and when they signed it off — so the approver can see level 1 happened rather than
assume it. Below the table, one proposal at a time with a "Signing off 2 of 3" counter;
sign it off and the next appears.

On the supplied set that produces **12 documents received, 3 duplicates skipped,
226 clauses, 53 actionable, 53 proposed changes**.

## Every document type, including documents inside documents

ABL confirmed circulars arrive in every format. The MVP handles all of them:

| Format | How |
|---|---|
| Native-text PDF | text layer, no OCR |
| Scanned PDF · images (png, jpg, tif, bmp) | **Tesseract OCR** |
| **Mixed PDF** | decided **per page** — part text layer, part scanned |
| Word | paragraphs **and tables** |
| Excel | every sheet and row, with `[row 14]` references kept |
| **Word containing Excel** | the embedded workbook becomes a document in its own right |
| Legacy `.doc .xls .ppt` | converted with LibreOffice |
| Outlook `.msg`, email, HTML, RTF, CSV, text | read directly |

Three rules, the same ones the delivered system follows:

- **Recursion is capped** — depth 3, hashes tracked so a self-referencing container
  cannot loop, and a total size cap so one bad file fails the document not the batch.
- **The extension is not trusted** — magic bytes decide. A `.pdf` that is really a Word
  file is normal from a mail system, and is read correctly.
- **Nothing is dropped silently** — password-protected, corrupt or empty files are
  stored with their reason and shown on the dashboard. A circular that disappears
  quietly is the worst outcome, because the team believes it was processed.

`_format_test/` holds one circular of each type, including a Word file with an embedded
spreadsheet, a file whose extension lies, a corrupt file and an empty one. Copy them
into `Circulars Data/` and re-run the pipeline to see every case handled.

## RIA emails

ABL confirmed the content may arrive either in the email body or as an attachment. Both
are handled, and each document records which route it came by. The three sample emails
in `RIA Emails/` cover:

- body only — the clauses written into the message
- covering note plus an attached circular
- body clauses **and** an attachment

The attached Branchless Banking document is also in the circulars folder, so the demo
shows the same document arriving by two routes being de-duplicated.

## Outputs

**Excel** — `output/Audit_Checklist_Working_File.xlsx`

| Sheet | Contents |
|---|---|
| Summary | Department-wise counts — BA, MA, IS&CA, RR, SA |
| Proposed Tests | The working rows. Additions in green text, deletions in red strike-through, per BRD §5.6 |
| Week MIS | Every document received, actionable Yes/No, action taken |

**Word** — one working document per circular, each clause carrying a note in the audit
team's own convention: "New test …", "Covered in …", or "Information".

---

## What is real and what is a demo shortcut

| | Real system | This MVP |
|---|---|---|
| Documents | ABL's own | **the same — real** |
| Audit library | 7,500 live tests | **500 generated samples** |
| Database | PostgreSQL 18 + pgvector | one SQLite file |
| Embeddings | BGE-large, 1024-d, 3.5 GB | TF-IDF vectors in numpy, no download |
| OCR | Qwen2.5-VL, Tesseract fallback | not needed — every supplied file has a text layer |
| Decision | 72B model on the H100 | deterministic rules, or local Ollama with `--engine ollama` |
| Excel | 6 sheets, 78 columns | 3 sheets, the columns that tell the story |
| Approval | Reviewer → Approver, RBAC, audit log, versioning | **two levels present**; no users, roles or audit log |
| eAudit export | quarterly BAC file, refuses if anything is unapproved | **present**; exports approved rows and names the rest |

**Five rules are kept exactly as proposed**, because they are the ones the client is
buying rather than presentation details:

1. **Nothing is exported without human approval**, at two levels. Level 1 only advances
   a proposal — it never finalises it.
2. **Every cited test code is validated against the library** before a reviewer sees it.
   A code that cannot be validated is dropped, not displayed.
3. **Deletions are flagged for explicit confirmation** and never auto-approved.
4. **Every proposal traces back to its source clause**, with page and character position.
5. **Excel is an output, not the database.** Editing an exported file changes nothing.

## Options

```bash
python run_pipeline.py --setup            # rebuild the 500-test library only
python run_pipeline.py --engine ollama    # use a local model for the decision step
```

`--engine ollama` expects Ollama on `localhost:11434` with `qwen2.5:7b`. It is slower
and non-deterministic; the default rules engine is the one to demo with.

## Layout

```
MVP/
├── README.md             this file
├── run_pipeline.py       the whole pipeline, one command
├── app.py                the Streamlit demo — six tabs
├── simple_app.py            the simple one-screen demo — table, sign-offs, export
├── mvp/
│   ├── config.py         paths, sizes, ABL vocabulary
│   ├── store.py          SQLite schema and helpers
│   ├── dummy_data.py     generates the 500 sample tests
│   ├── extract.py        PDF / DOCX / text
│   ├── ingest.py         folders + RIA emails, de-duplication
│   ├── segment.py        clause splitting, actionability
│   ├── retrieve.py       BM25 + vectors, fused
│   ├── decide.py         proposals + code validation
│   ├── word_out.py       Word working document
│   ├── excel_out.py      Excel working file
│   └── review.py         two-level review + eAudit export
├── Circulars Data/       ABL's documents
├── RIA Emails/           sample emails, both scenarios
└── output/               generated files
```

## Resetting

Delete `mvp_demo.db` and `output/`, then run the pipeline again. Everything is rebuilt
from the documents and the generator — nothing is hand-maintained.

