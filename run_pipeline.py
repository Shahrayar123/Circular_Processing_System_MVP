"""Run the whole MVP end to end.

    python run_pipeline.py              # full run: setup, ingest, decide, outputs
    python run_pipeline.py --setup      # only rebuild the 500-test library and index
    python run_pipeline.py --engine ollama   # use a local model for the decision step
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
    print()
    print(text)
    print("-" * len(text))


def setup_library() -> None:
    banner("1 · Audit test library")
    store.init(reset=True)
    tests = dummy_data.generate()
    with store.connect() as conn:
        store.insert_many(conn, "audit_tests", tests)
    print(f"   {len(tests)} dummy audit tests generated "
          f"({len({t['test_description'] for t in tests})} distinct descriptions)")

    started = time.time()
    count = retrieve.build_and_store()
    print(f"   {count} indexed for search in {time.time() - started:.1f}s "
          f"(BM25 + vectors, no model download)")


def run_intake() -> None:
    banner("2 · Intake — circular directory and RIA mailbox")
    results = ingest.run()

    ingested = [r for r in results if r["status"] == "ingested"]
    duplicates = [r for r in results if r["status"] == "duplicate"]
    failed = [r for r in results if r["status"] == "error"]

    for r in ingested:
        nested = "  (embedded in its parent)" if r.get("parent_id") else ""
        ocr = f"  [{r['ocr_pages']} page(s) OCR'd]" if r.get("ocr_pages") else ""
        print(f"   ingested   {r['filename'][:44]:<46} {r.get('kind', ''):<13} "
              f"{r['chars']:>7,} chars{ocr}{nested}")
    for r in duplicates:
        print(f"   DUPLICATE  {r['filename'][:44]:<46} same content as "
              f"{r['duplicate_of'][:34]}")
    for r in failed:
        # Recorded, never dropped — a circular that disappears quietly is the worst
        # outcome, because the audit team believes it was processed.
        print(f"   FAILED     {r['filename'][:44]:<46} {r['error'][:60]}")

    print(f"\n   {len(ingested)} ingested, {len(duplicates)} duplicates skipped, "
          f"{len(failed)} could not be read")
    nested_n = sum(1 for r in ingested if r.get("parent_id"))
    ocr_n = sum(r.get("ocr_pages", 0) for r in ingested)
    if nested_n or ocr_n:
        print(f"   {nested_n} document(s) found inside other documents · "
              f"{ocr_n} page(s) read by OCR")

    banner("3 · Clause extraction")
    with store.connect() as conn:
        for r in ingested:
            found = segment.segment_document(conn, r["document_id"], r["text"])
            actionable = conn.execute(
                "SELECT COUNT(*) FROM clauses WHERE document_id = ? AND is_actionable = 1",
                (r["document_id"],)).fetchone()[0]
            print(f"   {r['filename'][:52]:<54} {found:>3} clauses, {actionable:>3} actionable")


def run_decisions(engine: str) -> None:
    banner(f"4 · Matching and proposals  (engine: {engine})")
    index = retrieve.load_index()
    summary = decide.decide_all(index, engine=engine)
    for change_type, count in sorted(summary["by_type"].items()):
        print(f"   {change_type:<12} {count:>4}")
    print(f"   {'TOTAL':<12} {summary['proposals']:>4}")
    if summary["dropped_invalid_code"]:
        print(f"   {summary['dropped_invalid_code']} proposal(s) dropped — "
              f"cited test code not in the library")


def run_outputs() -> None:
    banner("5 · Outputs")
    excel = excel_out.build()
    print(f"   Excel  {excel.name}")
    for path in word_out.build_all():
        print(f"   Word   {path.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="rebuild the library only")
    parser.add_argument("--engine", default=config.DECISION_ENGINE,
                        choices=sorted(decide.ENGINES), help="decision engine")
    args = parser.parse_args()

    print("=" * 66)
    print("  AuditPilot MVP — demonstration run")
    print("  Dummy 500-test library · ABL's own documents · no live systems")
    print("=" * 66)

    started = time.time()
    setup_library()
    if args.setup:
        print("\nlibrary ready.")
        return 0

    run_intake()
    run_decisions(args.engine)
    run_outputs()

    counts = store.counts()
    banner("Done")
    print(f"   {counts['documents']} documents · {counts['duplicates']} duplicates · "
          f"{counts['nested']} nested · {counts['failed']} unreadable · "
          f"{counts['ocr_pages']} OCR pages")
    print(f"   {counts['clauses']} clauses · {counts['actionable']} actionable · "
          f"{counts['proposals']} proposals")
    print(f"   {time.time() - started:.1f}s total")
    print(f"\n   Outputs in {config.OUTPUT_DIR}")
    print("   Now run:  streamlit run app.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
