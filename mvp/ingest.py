"""Intake — the circulars folder and the RIA mailbox.

Two things this demonstrates, both of which the real system does the same way:

1. **De-duplication by SHA-256.** The supplied set contains two identical pairs; they
   are detected and skipped rather than processed twice.
2. **RIA emails arrive in both shapes.** Per ABL: the relevant content may be in the
   email body itself, or in an attached document. Both are handled, and the source is
   recorded so a reviewer can see which route an item came in by.
3. **Documents can contain documents.** A Word file carrying an embedded spreadsheet
   yields both — the child is recorded against its parent, so a reviewer sees that a
   clause came from "Annexure B, inside BPRD Circular 07".
4. **Nothing is dropped silently.** A file that cannot be read is stored with its error
   and shows in the queue as needing attention.
"""

import email
import hashlib
from datetime import datetime
from email.message import Message
from pathlib import Path

from . import config, extract, store


def file_hash(path: Path) -> str:
    """SHA-256 of the file's CONTENTS. De-duplication is on content, never on filename,
    so the same circular arriving twice or by two routes is worked once.
    """
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_hash(text: str) -> str:
    """SHA-256 of extracted text — used where there is no file on disk, such as an email body."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _skip(path: Path) -> bool:
    return path.name.startswith((".", "~$")) or path.suffix.lower() not in extract.SUPPORTED


def _already_processed(conn, filename: str, hash_value: str) -> dict | None:
    """The outcome for a file an earlier run already handled, or None to go on and read it.

    Matched on hash AND filename together, deliberately. Hash alone would swallow a
    genuine second copy arriving under a new name; filename alone would miss a circular
    reissued with corrected content under the same name — which must be re-read, and is,
    because the hash no longer matches.

    A row is only proof of completion if `processed_at` is set. The document row is
    written at intake, before splitting, so a run that died in between leaves a row with
    nothing behind it — and treating that as done is how a circular disappears for good,
    skipped on every future run with no error anywhere.
    """
    seen = conn.execute(
        "SELECT id, status FROM documents WHERE file_hash = ? AND filename = ?",
        (hash_value, filename),
    ).fetchone()
    if not seen:
        return None

    if store.is_complete(conn, seen["id"]):
        return {"status": "unchanged", "filename": filename,
                "document_id": seen["id"], "previous_status": seen["status"]}

    if store.reclaim(conn, seen["id"]):
        return None                     # half-finished and untouched: read it properly now

    # Someone has acted on part of it. Leave it alone and say so — a half-finished
    # document with human decisions on it needs a person, not a retry.
    return {"status": "unchanged", "filename": filename,
            "document_id": seen["id"], "previous_status": seen["status"],
            "note": "incomplete, but a human has already acted on it"}


def _record_duplicate(conn, existing, *, filename, source, source_detail, hash_value,
                      pages, parent_id, kind) -> dict:
    """Record that the same CONTENT arrived again under a different name.

    Kept rather than ignored: a circular sent twice is a real event in the week's intake
    and the team should see it.
    """
    dup_id = store.insert(conn, "documents", {
        "filename": filename, "source": source, "source_detail": source_detail,
        "file_hash": hash_value, "title": None, "doc_date": None, "pages": pages,
        "status": "duplicate", "duplicate_of": existing["id"],
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
        "parent_id": parent_id, "kind": kind, "ocr_pages": 0, "error": None,
    })
    # A duplicate is finished the moment it is recognised — there is nothing else to do
    # with it. Marked complete here, or the next run would see an unprocessed row, reclaim
    # it and record the same duplicate all over again, every run for ever.
    store.mark_processed(conn, dup_id)
    return {"status": "duplicate", "filename": filename,
            "duplicate_of": existing["filename"]}


def _record(conn, *, filename, source, source_detail, hash_value, text, pages,
            parent_id=None, kind="", ocr_pages=0, error="") -> dict:
    """Record one document. Returns one of three outcomes:

    * **unchanged** — this exact file was processed by an earlier run. Its clauses and
      proposals are already in the database, possibly reviewed and approved. Nothing is
      written and nothing is re-processed.
    * **duplicate** — the same CONTENT arrived under a different name. Recorded.
    * **ingested** — new. This is the only outcome that leads to clause extraction.
    """
    outcome = _already_processed(conn, filename, hash_value)
    if outcome:
        return outcome

    existing = conn.execute(
        "SELECT id, filename FROM documents WHERE file_hash = ? AND status != 'duplicate'",
        (hash_value,),
    ).fetchone()
    if existing:
        return _record_duplicate(conn, existing, filename=filename, source=source,
                                 source_detail=source_detail, hash_value=hash_value,
                                 pages=pages, parent_id=parent_id, kind=kind)

    # A file that could not be read is STORED WITH ITS ERROR, never dropped. A circular
    # that vanishes silently is the worst outcome — the team believes it was processed.
    status = "error" if error and not text.strip() else "ingested"

    doc_id = store.insert(conn, "documents", {
        "filename": filename, "source": source, "source_detail": source_detail,
        "file_hash": hash_value,
        "title": extract.guess_title(text, filename),
        "doc_date": extract.guess_date(text),
        "pages": pages, "status": status, "duplicate_of": None,
        "ingested_at": datetime.now().isoformat(timespec="seconds"),
        "parent_id": parent_id, "kind": kind, "ocr_pages": ocr_pages,
        "error": error or None,
    })

    outcome = {"status": status, "filename": filename, "document_id": doc_id,
               "chars": len(text), "pages": pages, "kind": kind,
               "ocr_pages": ocr_pages, "error": error, "parent_id": parent_id}

    # A reissue changes work already in the reviewer's queue, so it is reported back to
    # the caller rather than handled quietly here.
    replaced = _find_reissued(conn, filename, doc_id, parent_id)
    if replaced:
        outcome["supersedes"] = replaced["filename"]
        outcome["supersession"] = store.supersede(conn, replaced["id"], doc_id)
    return outcome


def _find_reissued(conn, filename: str, new_id: int, parent_id) -> dict | None:
    """The completed top-level document this file replaces, if it is a reissue.

    A reissue is the same FILENAME with different content — SBP sends "BPRD Circular
    No. 09 of 2026.pdf" again with a clause corrected, and the bank saves over the old
    one. Different content means a different hash, so it has already been read as a new
    document by the time this runs; all that is missing is the link.

    Restricted to top-level documents on purpose. Embedded children are matched on the
    filename their container gives them — "Microsoft_Excel_Worksheet.xlsx", "Annexure
    B.docx" — which repeats across unrelated circulars, so filename equality between two
    children means nothing. A changed annexure is instead superseded through its parent.

    Returns None when there is nothing to supersede, which is the normal case.
    """
    if parent_id is not None:
        return None
    row = conn.execute(
        "SELECT id, filename FROM documents "
        "WHERE filename = ? AND id != ? AND parent_id IS NULL "
        "  AND status = 'ingested' AND processed_at IS NOT NULL "
        # Only the newest surviving version — a circular reissued three times must form a
        # chain, not have every earlier version point at the latest one.
        "  AND superseded_by IS NULL "
        "ORDER BY id DESC LIMIT 1", (filename, new_id)).fetchone()
    return dict(row) if row else None


def _record_tree(conn, *, filename, source, source_detail, hash_value,
                 result, parent_id=None) -> list[dict]:
    """Record one document and, recursively, every document found inside it.

    ABL's own BRD is a .docx carrying an .xlsm and four .docx files. The embedded
    workbook is the part that matters, so a child is a document in its own right —
    it gets clauses, proposals and a place in the queue like any other.
    """
    outcome = _record(
        conn, filename=filename, source=source, source_detail=source_detail,
        hash_value=hash_value, text=result.text, pages=result.pages,
        parent_id=parent_id, kind=result.kind, ocr_pages=result.ocr_pages,
        error=result.error)
    outcome["text"] = result.text
    rows = [outcome]

    parent_doc_id = outcome.get("document_id")
    for child_name, child in result.children:
        if parent_doc_id is None:          # the parent was a duplicate — skip its children
            break
        rows += _record_tree(
            conn,
            filename=child_name,
            source=f"{source} · embedded",
            source_detail=f"inside {filename}",
            hash_value=text_hash(child.text or child_name),
            result=child,
            parent_id=parent_doc_id)
    return rows


# ====== SOURCE: FOLDER ======

def scan_folder(conn) -> list[dict]:
    """Read every circular in the watched folder and record it. Returns one result per file.

    Word's `~$` lock files and unsupported types are skipped before any reading happens.
    """
    results = []
    for path in sorted(config.CIRCULARS_DIR.glob("*")):
        if path.is_dir() or _skip(path):
            continue
        results += _record_tree(
            conn, filename=path.name, source="folder",
            source_detail=str(path.parent.name), hash_value=file_hash(path),
            result=extract.read(path))
    return results


# ====== SOURCE: RIA MAILBOX ======

def _body_text(message: Message) -> str:
    if not message.is_multipart():
        payload = message.get_payload(decode=True)
        return payload.decode("utf-8", "replace") if payload else str(message.get_payload())
    parts = []
    for part in message.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            payload = part.get_payload(decode=True)
            if payload:
                parts.append(payload.decode("utf-8", "replace"))
    return "\n".join(parts)


def scan_emails(conn) -> list[dict]:
    """Read RIA emails. Content may be in the body or in an attachment — both count."""
    results = []
    for path in sorted(config.EMAILS_DIR.glob("*.eml")) + sorted(config.EMAILS_DIR.glob("*.txt")):
        raw = path.read_bytes()
        message = email.message_from_bytes(raw)
        subject = (message.get("Subject") or path.stem).strip()
        sender = (message.get("From") or "unknown").strip()
        body = _body_text(message)

        # 1. attachments — the circular arrived as a file
        attached = False
        for part in message.walk():
            name = part.get_filename()
            if not name or Path(name).suffix.lower() not in extract.SUPPORTED:
                continue
            payload = part.get_payload(decode=True) or b""
            saved = config.EMAILS_DIR / f"_attachments/{name}"
            saved.parent.mkdir(parents=True, exist_ok=True)
            saved.write_bytes(payload)
            rows = _record_tree(
                conn, filename=name, source="email-attachment",
                source_detail=f"{subject} — from {sender}",
                hash_value=hashlib.sha256(payload).hexdigest(),
                result=extract.read(saved))
            for row in rows:
                row["email"] = path.name
            results += rows
            attached = True

        # 2. body — the clauses were written into the message itself
        #    Only treated as a document when there is enough of it to be worth reading,
        #    otherwise a covering note for an attachment would become its own circular.
        if len(body.strip()) >= 400 or not attached:
            outcome = _record(
                conn, filename=f"{path.stem}.txt", source="email-body",
                source_detail=f"{subject} — from {sender}",
                hash_value=text_hash(body), text=body, pages=1, kind="email-body")
            outcome["text"] = body
            outcome["email"] = path.name
            results.append(outcome)

    return results


def run() -> list[dict]:
    """Ingest everything — the circular folder and the RIA mailbox — and return every result.

    A result is one of: ingested, duplicate, or error. Failures are RETURNED, never
    raised: a circular that vanishes quietly is the worst outcome, because the team
    believes it was processed.
    """
    config.ensure_dirs()
    with store.connect() as conn:
        return scan_emails(conn) + scan_folder(conn)
