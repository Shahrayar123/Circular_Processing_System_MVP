# Circular Processing System MVP

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
| **PowerPoint** | slide text, **slide tables**, grouped shapes and speaker notes |
| **Word containing Excel** | the embedded workbook becomes a document in its own right |
| Legacy `.doc .xls .ppt` | converted with LibreOffice — the only format needing software pip cannot install |
| Outlook `.msg`, email, HTML, RTF, CSV, text | read directly |

**Every reader emits a table row in one format** — `[row 4] A-1 | Banks shall … | 01-Oct`
— because the clause splitter splits on that marker. A reader emitting a bare
`A | B | C` line produces rows the splitter cannot see, and they are not merely lost:
they are appended to the **preceding** clause, so its text ends with a run of table cells
and it retrieves badly for a reason nobody goes looking for. Word, Excel and PowerPoint
all go through `extract.table_row()` so this cannot drift apart again.

Three rules, the same ones the delivered system follows:

- **Recursion is capped** — depth 3, hashes tracked so a self-referencing container
  cannot loop, and a total size cap so one bad file fails the document not the batch.
- **The extension is not trusted** — magic bytes decide. A `.pdf` that is really a Word
  file is normal from a mail system, and is read correctly.
- **Nothing is dropped silently** — password-protected, corrupt or empty files are
  stored with their reason and shown on the dashboard. A circular that disappears
  quietly is the worst outcome, because the team believes it was processed.

`_format_test/` holds one circular of each type — eleven fixtures, including a Word file
with an embedded spreadsheet, a **PowerPoint whose obligations sit in a slide table and
in a grouped shape**, a file whose extension lies, a corrupt file and an empty one. Copy
them into `Circulars Data/` and re-run the pipeline to see every case handled.

Not yet proven on a real file: **`.msg`**. The reader and the library are both in place
and a malformed file degrades correctly, but no genuine Outlook message has been through
it — ABL has been asked for one (open question 17). Everything else in the table above
has been run end to end.

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
python run_pipeline.py --setup            # build the 500-test library only
python run_pipeline.py --judge llm        # a local model judges each clause
python run_pipeline.py --engine ollama    # a local model makes the change decision
python run_pipeline.py --reset            # DELETE the database, then run from scratch
python run_pipeline.py --reset-only       # DELETE the database and STOP
python run_pipeline.py --outputs all      # rewrite every Word working document
```

**To test the same circular repeatedly under the debugger**, wipe first and run second —
`--reset` does both in one process, and you cannot put a breakpoint in a run that has
already happened:

```bash
python run_pipeline.py --reset-only    # clean slate, nothing rebuilt
```

then F5 on **Pipeline — full run (rules)**, which now starts from empty. Both wipes also
delete the generated files in `output/`: document ids restart at 1, so a leftover
`01_..._working.docx` from the previous run would sit beside the new one with nothing to
tell them apart. Files you put there yourself are left alone.

Close the Streamlit app first — it holds the database open, and the wipe says so rather
than failing with a Windows error code.

## Layout

```
MVP/
├── README.md             this file
├── run_pipeline.py       the whole pipeline, one command
├── app.py                the Streamlit demo — six tabs
├── inspect_db.py         read the database from the command line (read-only)
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

## Running it on another machine

The database is **generated, not committed** — `mvp_demo.db` is in `.gitignore`, along
with the client documents. So on a fresh clone:

```bash
pip install -r requirements.txt
python run_pipeline.py
streamlit run simple_app.py
```

`run_pipeline.py` builds the 500-test library, reads whatever is in `Circulars Data/`
and writes the outputs. **It must be run before either app**, on every machine.

> **`no such table: audit_tests`** means the pipeline has not been run there yet. SQLite
> creates the database file the moment anything connects to it, so an empty
> `mvp_demo.db` can be left behind by a single query — delete it and run the pipeline.

Note that `Circulars Data/` is empty on a clone: the ABL documents are deliberately not
published. Drop any PDF, Word or Excel file in there — or copy `_format_test/` in — and
re-run the pipeline.

## One sentence, several obligations

A single clause routinely carries more than one duty:

> *"Banks shall verify every agent through NADRA before onboarding, **shall** maintain a
> register reconciled monthly, and **shall not** permit an agent to operate under more
> than one code."*

That is three obligations, three different audit tests, and three proposals. Treating the
clause as the unit loses two of them with no error — and it degrades retrieval, because
embedding a compound sentence gives the **centroid** of its meanings. Measured on the demo
library, that clause's best match dropped from **0.700** to **0.504**, and another
obligation's best test fell from rank 1 to **rank 6** — outside the top-K, so the decision
step never saw it.

So the obligation, not the clause, is the unit of work:

- The **clause text is never split** — it stays the unit of traceability, with one
  reference, one page and one pair of character offsets.
- Each obligation is its **own retrieval query**, its **own proposal**, and is **approved
  on its own** — one may be an Amendment while another is New.
- All the proposals from one clause share its reference, so the Excel shows three rows
  pointing at the same place in the circular.

The rules engine only splits the **explicit** case — duties joined by "and shall",
"; shall". That is narrow deliberately: deciding where one duty ends is a language
judgement, and a keyword split that tried to be clever would fragment ordinary prose. In
the delivered system the model returns the list directly.

## When the split looks wrong

Clause splitting is pattern-based, and patterns cannot cover every circular. A document
with no numbering, a two-column layout, or OCR that lost the indentation will not raise
an error — it returns one enormous clause or hundreds of fragments, and every number
after it degrades quietly. Three things make that visible:

- **Every clause records which rung split it** — `segmented_by`: `structure` (numbering,
  headings, table rows) or `obligation-lines` (the fallback). Anything but `structure`
  means the patterns did not fit.
- **Five sanity checks flag the document** rather than failing it: one clause for a long
  document · hundreds of tiny fragments · a median clause under 80 characters · a single
  clause over 4,000 · a clause count that moves between runs.
- **Nothing is truncated silently.** The clause is stored whole; only what is *sent to a
  model* is shortened, and the clause records that it happened. The delivered system
  faces the same question at the embedding step — `bge-large` cuts at 512 tokens without
  a warning — where the answer is overlapping windows indexed back to the parent clause.

The pipeline prints both, and the Documents tab shows them on the document itself.

## Reading the code in a debugger

The entry point is **`run_pipeline.py`**. Everything the system does is reachable from
its `main()`, in order, so a single debug session walks the whole pipeline.

`.vscode/launch.json` has the configurations ready — press **F5** and pick
**"Pipeline — full run (rules)"**. It needs no model and finishes in about five seconds.

Set these nine breakpoints and you will stop once at the start of each stage, in the
order they actually run:

| # | Breakpoint | What you are looking at |
|---|---|---|
| 1 | `run_pipeline.py:141` | The four stages, about to run. Step **over** each to see the shape of the whole run first. |
| 2 | `mvp/extract.py:470` `read()` | One file being read. Watch `sniff()` decide the real type, then the per-page routing inside `read_pdf`. |
| 3 | `mvp/extract.py:423` `_embedded_in_ooxml()` | A document being opened to look for documents inside it. |
| 4 | `mvp/segment.py:193` `split_clauses()` | Raw text becoming clauses — numbering, table rows, lead-in lists. |
| 5 | `mvp/segment.py:252` `classify()` | The actionability judgement. `--judge llm` sends you to `classify_llm()` at line 382 instead. |
| 6 | `mvp/segment.py:460` `split_obligations()` | One clause being checked for several duties. |
| 7 | `mvp/retrieve.py:100` `Index.search()` | The search itself: BM25 scores, cosine scores, then Reciprocal Rank Fusion. |
| 8 | `mvp/decide.py:265` `decide_all()` | The loop over obligations — **not** clauses. |
| 9 | `mvp/decide.py` `_decide_rules()` | One decision being made: supersession first, then the cosine thresholds. |

**Two conditional breakpoints worth setting** (right-click the breakpoint → Edit
Breakpoint → Expression). Both stop only on the interesting case rather than the first
of 178:

```
len(found) > 1                         # segment.py:460 — a clause with several duties
decision["change_type"] == "Deletion"  # decide.py, after the decision is built
```

**Things worth knowing before you start:**

- **The first stage does NOT delete anything.** The library is built once, and a
  document already in the database is skipped rather than re-read. Only `--reset` wipes,
  and it says so before it does. Set a breakpoint in `setup_library()` and run twice to
  watch the second run skip everything.
- **Watch these in the Variables pane:** `Extracted.kind` and `.children` at stage 2,
  `clause["text"]` at 4–6, and `score_dense` versus `score_fused` at 7 — the first
  carries meaning, the second only carries rank.
- **Use the Debug Console** to run expressions against live state, e.g.
  `[c["test_code"] for c in candidates]` while stopped inside `_decide_rules`.
- **`justMyCode` is false** in these configurations, so you can step into `pypdf` or
  `rank_bm25` when you want to. Set it to true to stay in our own code.

## Looking inside the database

Everything the demo knows lives in `mvp_demo.db`. To read it without the UI:

```bash
python inspect_db.py                          # summary + every section
python inspect_db.py show 2026-W36-26-IB26    # one change, before and after, in full
python inspect_db.py sql "SELECT ..."         # any SELECT
```

Sections: `documents` (format, nesting, failures), `clauses` (actionability and **who
judged it**), `proposals`, `edits` (what a human changed, from what), `approvals`.

It never writes — a non-SELECT is refused. Useful on the client machine where no SQLite
browser is installed, and for answering "where is that actually stored" without opening
Streamlit.

## Resetting

Delete `mvp_demo.db` and `output/`, then run the pipeline again. Everything is rebuilt
from the documents and the generator — nothing is hand-maintained.

