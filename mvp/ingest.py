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
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _skip(path: Path) -> bool:
    return path.name.startswith((".", "~$")) or path.suffix.lower() not in extract.SUPPORTED


def _record(conn, *, filename, source, source_detail, hash_value, text, pages,
            parent_id=None, kind="", ocr_pages=0, error="") -> dict:
    """Insert a document, or mark it a duplicate of one already seen."""
    existing = conn.execute(
        "SELECT id, filename FROM documents WHERE file_hash = ? AND status != 'duplicate'",
        (hash_value,),
    ).fetchone()

    if existing:
        store.insert(conn, "documents", {
            "filename": filename, "source": source, "source_detail": source_detail,
            "file_hash": hash_value, "title": None, "doc_date": None, "pages": pages,
            "status": "duplicate", "duplicate_of": existing["id"],
            "ingested_at": datetime.now().isoformat(timespec="seconds"),
            "parent_id": parent_id, "kind": kind, "ocr_pages": 0, "error": None,
        })
        return {"status": "duplicate", "filename": filename,
                "duplicate_of": existing["filename"]}

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
    return {"status": status, "filename": filename, "document_id": doc_id,
            "chars": len(text), "pages": pages, "kind": kind,
            "ocr_pages": ocr_pages, "error": error, "parent_id": parent_id}


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
    config.ensure_dirs()
    with store.connect() as conn:
        return scan_emails(conn) + scan_folder(conn)
