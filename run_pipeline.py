"""Run the whole MVP end to end.

    python run_pipeline.py              # full run: setup, ingest, decide, outputs
    python run_pipeline.py --setup      # only build the 500-test library and index
    python run_pipeline.py --engine ollama   # use a local model for the decision step
    python run_pipeline.py --reset      # DELETE the database, then run from scratch
    python run_pipeline.py --reset-only # DELETE the database and STOP, ready to debug
    python run_pipeline.py --outputs all     # rewrite every Word working document

The whole pipeline is the five calls in `main()`, in order, each one a stage:

    setup_library()      the 500-test library, built once            -> audit_tests
    read_documents()     circulars and emails in, de-duplicated      -> documents
    extract_clauses()    each document split, each clause judged     -> clauses, obligations
    propose_changes()    each obligation matched against the library -> proposals
    write_outputs()      the Excel file, and Word docs for THIS run   -> output/

Each stage reads what the one before it wrote to the database and writes its own rows.
Nothing is held in memory between stages except the list of documents just ingested,
which `read_documents()` returns so `extract_clauses()` knows which ones are new.

A run ADDS to the database; it never replaces it. The library is built once, a document
already processed is skipped on its content hash, and an obligation that already has a
proposal is left alone. That is what lets the pipeline run again on Tuesday without
destroying the review work done on Monday — and it is how the live system must behave.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from mvp import config, decide, dummy_data, excel_out, ingest, retrieve, segment, store, word_out  # noqa: E402


def banner(text: str) -> None:
    """Print a section heading, so a long run stays readable."""
    print()
    print(text)
    print("-" * len(text))


# ====== STAGE 1 · THE AUDIT TEST LIBRARY ======

def setup_library(force: bool = False) -> None:
    """Build the audit test library and index it — ONCE, not on every run.

    Loading the library is a one-off setup step, exactly as it is in the live system: the
    library is a slow-moving asset that outlives any single circular. Rebuilding it every
    run would also renumber every test, orphaning the `target_test_code` on every proposal
    already reviewed.

    `force=True` (the `--reset` flag) rebuilds it anyway, for a clean demo.
    """
    banner("1 · Audit test library")
    store.init(reset=force)
    if store.library_loaded() and not force:
        print(f"   {store.counts()['tests']} audit tests already loaded and indexed "
              f"— skipping (use --reset to rebuild)")
        return

    tests = dummy_data.generate()
    with store.connect() as conn:
        store.insert_many(conn, "audit_tests", tests)
    print(f"   {len(tests)} dummy audit tests generated "
          f"({len({t['test_description'] for t in tests})} distinct descriptions)")

    started = time.time()
    count = retrieve.build_and_store()
    print(f"   {count} indexed for search in {time.time() - started:.1f}s "
          f"(BM25 + vectors, no model download)")


# ====== STAGE 2 · INTAKE ======

def read_documents() -> list[dict]:
    """Read every circular and email, record each one, and return the NEW ones.

    Reading and recording only — no clause splitting happens here. The returned list is
    the documents this run actually has work to do on: duplicates, documents already
    processed by an earlier run, and files that could not be read are all reported and
    then left behind.
    """
    banner("2 · Intake — circular directory and RIA mailbox")
    results = ingest.run()

    ingested = [r for r in results if r["status"] == "ingested"]
    unchanged = [r for r in results if r["status"] == "unchanged"]
    duplicates = [r for r in results if r["status"] == "duplicate"]
    failed = [r for r in results if r["status"] == "error"]

    for r in ingested:
        nested = "  (embedded in its parent)" if r.get("parent_id") else ""
        ocr = f"  [{r['ocr_pages']} page(s) OCR'd]" if r.get("ocr_pages") else ""
        print(f"   ingested   {r['filename'][:44]:<46} {r.get('kind', ''):<13} "
              f"{r['chars']:>7,} chars{ocr}{nested}")
        if r.get("supersession"):
            # A reissue is the one intake event that changes work already in the queue,
            # so it is reported per document rather than in the totals at the end.
            s = r["supersession"]
            print(f"      REISSUE of an earlier version — {s['withdrawn']} untouched "
                  f"proposal(s) withdrawn from the queue")
            if s["kept"]:
                print(f"      {s['kept']} proposal(s) from the old version were already "
                      f"reviewed or approved and were LEFT ALONE — check them against "
                      f"the new text")
    for r in unchanged:
        print(f"   unchanged  {r['filename'][:44]:<46} already processed by an earlier run")
    for r in duplicates:
        print(f"   DUPLICATE  {r['filename'][:44]:<46} same content as "
              f"{r['duplicate_of'][:34]}")
    for r in failed:
        # Recorded, never dropped — a circular that disappears quietly is the worst
        # outcome, because the audit team believes it was processed.
        print(f"   FAILED     {r['filename'][:44]:<46} {r['error'][:60]}")

    print(f"\n   {len(ingested)} ingested, {len(unchanged)} already processed, "
          f"{len(duplicates)} duplicates skipped, {len(failed)} could not be read")
    if not ingested:
        # Not an error — it is the normal result of running twice with nothing new in the
        # folder. Said plainly, because an empty stage 3 otherwise looks like a failure.
        print("   nothing new to process — everything in the folder is already in the "
              "database, with its review state intact")

    nested_n = sum(1 for r in ingested if r.get("parent_id"))
    ocr_n = sum(r.get("ocr_pages", 0) for r in ingested)
    if nested_n or ocr_n:
        print(f"   {nested_n} document(s) found inside other documents · "
              f"{ocr_n} page(s) read by OCR")

    return ingested


# ====== STAGE 3 · CLAUSES ======

def extract_clauses(documents: list[dict], judge: str) -> None:
    """Split each new document into clauses and judge which ones create an obligation.

    `documents` is what `read_documents()` returned — each carries the text already read
    from the file, so nothing is opened twice.

    `judge` selects WHO decides actionability: "rules" (keyword patterns, instant) or
    "llm" (a local model). It belongs to this stage and nowhere else; every clause records
    which judge ruled on it, because a run that quietly fell back to the rules while
    claiming to use the model is worse than one that never had a model.
    """
    banner(f"3 · Clause extraction  (actionability judged by: "
           f"{segment.JUDGES.get(judge, judge)})")

    # ONE connection for every document, not one each. Two reasons, and the second is the
    # one that matters: `with connect()` commits when the block exits, so all of this
    # run's clauses land together or not at all — a crash halfway through leaves no
    # half-written document behind for the next run to mistake for a finished one.
    with store.connect() as conn:
        for doc in documents:
            # `doc` came from read_documents(), so the file's text is already in memory.
            # Passing it on rather than re-reading from disk keeps the file opened once
            # per run — and on a scanned circular a re-read would mean OCR'ing it twice.
            summary = segment.segment_document(conn, doc["document_id"], doc["text"],
                                               judge=judge)

            # Every figure printed here comes back from segment_document, which had all
            # of them in hand. Nothing is queried back out of the database to print it.
            print(f"   {doc['filename'][:52]:<54} {summary['clauses']:>3} clauses, "
                  f"{summary['actionable']:>3} actionable")

            # A split that looks wrong is reported HERE, next to the document it concerns
            # — not left for someone to infer later from a low clause count on a screen
            # three steps away. It is a flag, never a failure: the document is stored
            # either way and a person decides whether the split is usable.
            if summary["warning"]:
                print(f"      CHECK THE SPLIT: {summary['warning']}")

            # Only NOW is the document finished, so only now is it marked. Marked at
            # intake instead, a crash anywhere above would leave a document that looks
            # complete, and every future run would skip it as already processed — losing
            # the circular silently, which is the worst failure this system has.
            store.mark_processed(conn, doc["document_id"])


# ====== STAGE 4 · PROPOSALS ======

def propose_changes(engine: str) -> None:
    """Match every actionable obligation against the library and store the proposals."""
    banner(f"4 · Matching and proposals  (engine: {engine})")
    index = retrieve.load_index()
    summary = decide.decide_all(index, engine=engine)

    for change_type, count in sorted(summary["by_type"].items()):
        print(f"   {change_type:<12} {count:>4}")
    print(f"   {'NEW THIS RUN':<12} {summary['proposals']:>4}")
    if not summary["proposals"]:
        # Says WHY the total is zero. Without this the stage looks broken on a re-run,
        # and the natural next move is to reach for --reset — which is the one command
        # that would actually destroy something.
        print("   every obligation already has a proposal — existing proposals, edits "
              "and approvals left untouched")
    # What the grounding gate corrected, if anything. Printed because a correction is not
    # a tidy-up: each one is a decision an engine got wrong in a way that would have
    # reached a reviewer, and the counts are how you notice an engine degrading.
    REASONS = {
        "unknown_type":   "change type not recognised",
        "invented_code":  "cited test code not in the library",
        "missing_target": "Amendment or Deletion with no test to act on",
        "empty_change":   "Amendment proposed the existing wording unchanged",
        "recovered_by_rules": "re-decided by the rules engine rather than dropped",
    }
    for reason, count in sorted(summary["corrected"].items()):
        print(f"   {count} proposal(s) corrected — {REASONS.get(reason, reason)}")


# ====== STAGE 5 · OUTPUTS ======

def write_outputs(documents: list[dict], regenerate_all: bool = False) -> None:
    """Write the Excel working file, and a Word document per circular THIS RUN processed.

    The two outputs have different scopes on purpose:

    * The **Excel working file is one consolidated file** covering every proposal under
      review, so it is rebuilt every run. There is only one of it, and it has to show the
      current state of everything.
    * A **Word working document belongs to one circular**, and is the copy the audit team
      annotates. Rewriting one because an unrelated circular arrived this week overwrites
      whatever they put in it, with nothing to say so — so only the circulars this run
      actually processed get rewritten.

    `regenerate_all=True` (the `--outputs all` flag) rewrites every one. Use it when
    review state has moved — proposals edited or approved — because those documents are
    then genuinely out of date.
    """
    banner("5 · Outputs")
    excel = excel_out.build()
    print(f"   Excel  {excel.name}   (consolidated, always rebuilt)")

    ids = None if regenerate_all else [d["document_id"] for d in documents]
    paths = word_out.build_all(ids)
    for path in paths:
        print(f"   Word   {path.name}")
    if not paths:
        print("   no Word document rewritten — nothing new was processed this run "
              "(use --outputs all to regenerate every one)")


def clear_generated_outputs() -> list[str]:
    """Delete the files the pipeline generates. Returns the names removed.

    Called only by `--reset`, and only for files this pipeline writes — a Word working
    document, the Excel working file, the eAudit export. Anything else someone put in the
    output folder is left alone.

    Without this, `--reset` leaves a genuinely misleading folder behind. Document ids
    restart at 1, so last run's `01_Some_Circular_working.docx` sits next to the new
    `01_A_Different_Circular_working.docx`, both looking current, and the stale Excel file
    still lists proposals that no longer exist in the database. `review.reset_all()`
    already deletes the eAudit export for exactly this reason: a generated file that
    outlives the data behind it implies work that was never done.
    """
    removed = []
    for path in sorted(config.OUTPUT_DIR.glob("*")):
        if path.name.endswith("_working.docx") or path.name in (
                "Audit_Checklist_Working_File.xlsx", "eAudit_BAC_Export.xlsx"):
            path.unlink()
            removed.append(path.name)
    return removed


# ====== REPORTING ======

def print_summary(started: float) -> None:
    """The closing figures, read back from the database rather than counted along the way.

    Reading them back is deliberate: it reports what was actually STORED, so a stage that
    printed a number and then failed to write it cannot look successful here.
    """
    counts = store.counts()
    banner("Done")
    print(f"   {counts['documents']} documents · {counts['duplicates']} duplicates · "
          f"{counts['nested']} nested · {counts['failed']} unreadable · "
          f"{counts['ocr_pages']} OCR pages")
    if counts.get("superseded"):
        print(f"   {counts['superseded']} document(s) superseded by a reissue · "
              f"{counts['withdrawn']} proposal(s) withdrawn")
    print(f"   {counts['clauses']} clauses · {counts['actionable']} actionable · "
          f"{counts['proposals']} proposals")

    for row in store.query("SELECT COALESCE(judged_by, 'rules') j, COUNT(*) n "
                           "FROM clauses GROUP BY j ORDER BY n DESC"):
        print(f"   actionability judged by {row['j']}: {row['n']} clause(s)")
    for row in store.query("SELECT COALESCE(segmented_by, 'structure') s, COUNT(*) n "
                           "FROM clauses GROUP BY s ORDER BY n DESC"):
        print(f"   split by {row['s']}: {row['n']} clause(s)")

    if counts.get("flagged_splits"):
        print(f"   {counts['flagged_splits']} document(s) flagged — CHECK THE SPLIT above")
    if counts.get("multi_obligation_clauses"):
        print(f"   {counts['obligations']} obligations from {counts['actionable']} clauses — "
              f"{counts['multi_obligation_clauses']} clause(s) state more than one duty")
    if counts.get("truncated_for_model"):
        print(f"   {counts['truncated_for_model']} clause(s) shortened for the model "
              f"(stored whole; see model_input_truncated)")

    print(f"   {time.time() - started:.1f}s total")
    print(f"\n   Outputs in {config.OUTPUT_DIR}")
    print("   Now run:  streamlit run app.py")


# ====== COMMAND LINE ======

def parse_args() -> argparse.Namespace:
    """Read the command-line flags. Nothing is processed here — this only decides HOW the
    run will behave, before any work starts.

    `description=__doc__` reuses this file's own docstring as the help text, so
    `python run_pipeline.py --help` prints the usage examples at the top of the file
    rather than a second copy of them that would drift out of date.
    """
    parser = argparse.ArgumentParser(description=__doc__)

    # A switch, not a value: present means True, absent means False. It stops the run
    # after the library is built, for when you want the index without the documents.
    parser.add_argument("--setup", action="store_true", help="rebuild the library only")

    # DESTRUCTIVE, and the only way to lose data here. Without it a run adds to what is
    # already in the database and leaves every reviewed and approved proposal alone; with
    # it the database file is deleted and rebuilt from the documents. Kept as an explicit
    # opt-in because the default used to be the other way round, and a second run of the
    # week quietly destroyed the first run's sign-offs.
    parser.add_argument("--reset", action="store_true",
                        help="DELETE the database, then run everything from scratch")

    # The wipe on its own, with nothing rebuilt. `--reset` couples wiping with a full run,
    # which is wrong when the run itself is what you want to step through: you cannot put
    # a breakpoint in a run that has already happened. This clears the slate and stops, so
    # the NEXT run starts from empty and can be launched under the debugger.
    parser.add_argument("--reset-only", action="store_true",
                        help="DELETE the database and generated files, then stop")

    # Which engine turns a matched obligation into a proposal. `choices` comes from the
    # ENGINES registry itself, so a typo fails immediately and lists the valid options —
    # without it, `--engine ollma` would silently fall through to the default and you
    # would spend an hour wondering why the model never ran. It also means adding an
    # engine is one dict entry, with no change needed here.
    parser.add_argument("--engine", default=config.DECISION_ENGINE,
                        choices=sorted(decide.ENGINES), help="decision engine")

    # Which circulars get a Word working document written. "new" (the default) writes one
    # for each circular THIS run processed; "all" rewrites every one in the database.
    # Default is "new" because that file is the audit team's working copy and rewriting it
    # overwrites their annotations — see write_outputs().
    parser.add_argument("--outputs", choices=("new", "all"), default="new",
                        help="write Word documents for this run's circulars (new) or "
                             "for every circular in the database (all)")

    # Who decides whether a clause creates an obligation: keyword rules, or the model.
    # Used by stage 3 only. Same registry-driven `choices` for the same reason.
    parser.add_argument("--judge", default=config.ACTIONABILITY_JUDGE,
                        choices=sorted(segment.JUDGES),
                        help="who decides whether a clause creates an obligation")

    # Reads sys.argv and returns a small object. With no flags that is
    # Namespace(setup=False, reset=False, engine='rules', judge='rules') — the defaults
    # come from config.py, so the demo runs with no model and no arguments at all.
    return parser.parse_args()


# ====== ENTRY POINT ======

def main() -> int:
    """Run the whole demo end to end. Returns a process exit code."""
    args = parse_args()

    print("=" * 66)
    print("  AuditPilot MVP — demonstration run")
    print("  Dummy 500-test library · ABL's own documents · no live systems")
    print("=" * 66)
    # Say what this run is configured to do. Without it, "judged by the model" in the
    # output does not say WHICH model, and a run in a screenshot cannot be reproduced.
    print(f"  {config.describe(judge=args.judge, engine=args.engine)}")
    # Both flags wipe; only --reset goes on to rebuild. The generated files go with the
    # database either way: document ids restart at 1, so leaving them would put a stale
    # "01_..." next to the new one with nothing to tell them apart.
    if args.reset or args.reset_only:
        print("  the existing database will be DELETED, including any review and")
        print("  approval decisions already recorded in it.")
        wiped = store.delete_database()
        removed = clear_generated_outputs()
        print(f"  database {'deleted' if wiped else 'was not there'} · "
              f"{len(removed)} generated file(s) removed from {config.OUTPUT_DIR.name}/")

    if args.reset_only:
        print("\n  clean slate. Nothing was rebuilt — the next run starts from empty")        
        return 0
    

    started = time.time()

    # The pipeline, in order. Read this and you have read the system.
    setup_library(force=args.reset)          # the wipe already happened above
    if args.setup:
        print("\nlibrary ready.")
        return 0

    documents = read_documents()                        # stage 2 -> the new documents
    extract_clauses(documents, judge=args.judge)        # stage 3 -> clauses, obligations
    propose_changes(engine=args.engine)                 # stage 4 -> proposals
    write_outputs(documents,                            # stage 5 -> Excel and Word
                  regenerate_all=args.outputs == "all")

    print_summary(started)
    return 0


if __name__ == "__main__":
    sys.exit(main())
