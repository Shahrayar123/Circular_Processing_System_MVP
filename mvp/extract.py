"""Read any document into text — including documents inside documents.

ABL confirmed circulars arrive in every format: scanned PDFs, images, native PDFs, Word,
Excel, Word containing Excel, legacy .doc/.xls, Outlook messages. Their own BRD proves
the nested case — it is a .docx carrying an .xlsm and four .docx files in
`word/embeddings/`, and the embedded workbook is the part that actually matters.

Three rules, the same ones the delivered system follows:

1. **Route per page, not per document.** A circular is routinely part native text and
   part scanned annexure. Deciding once for the whole file is wrong either way.
2. **Recurse into embedded objects**, with a depth cap, a seen-hash set and a size cap.
   A container that references itself must not loop; one bad file must not kill the run.
3. **Never fail silently.** Password-protected, corrupt, unsupported — the document is
   recorded with an error and surfaces in the queue. A circular that quietly disappears
   is the worst outcome: the audit team believes it was processed.
"""

import hashlib
import io
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xlsm", ".xls", ".pptx", ".ppt",
    ".txt", ".csv", ".htm", ".html", ".rtf", ".eml", ".msg",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif",
}

IMAGES = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif"}
LEGACY = {".doc": ".docx", ".xls": ".xlsx", ".ppt": ".pptx"}

MAX_DEPTH = 3                      # a document inside a document inside a document
MAX_TOTAL_CHARS = 4_000_000        # a 500 MB workbook fails the document, not the batch
NATIVE_TEXT_MIN_CHARS = 120        # per page, below this the page is treated as scanned

SOFFICE_PATHS = [
    Path(r"C:\Program Files\LibreOffice\program\soffice.exe"),
    Path(r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"),
    Path("/usr/bin/soffice"), Path("/usr/bin/libreoffice"),
]


@dataclass
class Extracted:
    """What one file yielded. `children` are documents found inside it."""
    text: str = ""
    pages: int = 0
    kind: str = ""                 # native-pdf | scanned-pdf | mixed-pdf | docx | xlsx …
    ocr_pages: int = 0
    error: str = ""
    children: list = field(default_factory=list)   # [(name, Extracted)]


# ====== HELPERS ======

def _clean(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\xa0", " ")
    text = _REVIEW_MARKUP.sub(" ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Word review markup rendered into a PDF when a draft is exported with comments and
# tracked changes showing. The parenthesised form is first and spans newlines, because
# PDF extraction wraps lines mid-marker.
_REVIEW_MARKUP = re.compile(
    r"Formatted:[^)\n]{0,80}\([^)]{0,200}\)"
    r"|Commented\s*\[[A-Za-z]+\d*\]:.*?(?=(?:\n|Commented\s*\[|Formatted:|$))"
    r"|Formatted:[^\n]*|Deleted:[^\n]*|Inserted:[^\n]*",
    re.DOTALL,
)


def find_soffice() -> Path | None:
    """Path to LibreOffice, or None. Used to convert legacy .doc/.xls/.ppt before reading.

    Absence is not an error: those formats are simply reported as unreadable, with the
    reason, rather than failing the run.
    """
    for path in SOFFICE_PATHS:
        if path.exists():
            return path
    found = shutil.which("soffice") or shutil.which("libreoffice")
    return Path(found) if found else None


def sniff(path: Path) -> str:
    """The real type from the magic bytes. Mail systems rename files freely — a .pdf
    that is actually a Word document is a normal thing to receive."""
    try:
        head = path.open("rb").read(8)
    except Exception:
        return path.suffix.lower()
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):                   # any OOXML / zip container
        suffix = path.suffix.lower()
        return suffix if suffix in {".docx", ".xlsx", ".xlsm", ".pptx"} else ".docx"
    if head.startswith(b"\xd0\xcf\x11\xe0"):             # legacy OLE compound file
        suffix = path.suffix.lower()
        return suffix if suffix in LEGACY or suffix == ".msg" else ".doc"
    if head[:3] in (b"\xff\xd8\xff",) or head.startswith(b"\x89PNG"):
        return path.suffix.lower() if path.suffix.lower() in IMAGES else ".png"
    return path.suffix.lower()


# ====== OCR ======

def ocr_image(image) -> str:
    """Tesseract. The delivered system uses Qwen2.5-VL with Tesseract as the failure
    fallback; the MVP has no vision endpoint, so Tesseract does the whole job."""
    import pytesseract

    for candidate in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                      r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
        if Path(candidate).exists():
            pytesseract.pytesseract.tesseract_cmd = candidate
            break
    return pytesseract.image_to_string(image)


# ====== READERS ======

def read_pdf(path: Path) -> Extracted:
    """Read a PDF, deciding OCR PER PAGE.

    A page with a healthy text layer is read directly; a page without one is rendered
    and OCR'd. Deciding for the whole document is the trap: a mixed PDF then either
    wastes OCR on clean pages or returns nothing for the scanned ones, and both are
    silent. `kind` reports which happened — native-pdf, scanned-pdf or mixed-pdf.
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages, native, scanned = [], 0, 0

    for index, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if len(text) >= NATIVE_TEXT_MIN_CHARS:
            native += 1
        else:
            # ROUTE PER PAGE. A part-scanned circular is normal; deciding for the whole
            # document wastes OCR on clean pages or silently loses the scanned ones.
            try:
                from pdf2image import convert_from_path
                images = convert_from_path(str(path), dpi=200,
                                           first_page=index + 1, last_page=index + 1,
                                           poppler_path=_poppler())
                if images:
                    text = (ocr_image(images[0]) or "").strip()
                    scanned += 1
            except Exception as exc:
                text = f"[page {index + 1}: could not be read — {exc}]"
        pages.append(text)

    kind = "native-pdf" if not scanned else ("scanned-pdf" if not native else "mixed-pdf")
    return Extracted(text=_clean("\n\n".join(pages)), pages=len(pages),
                     kind=kind, ocr_pages=scanned)


def _poppler():
    for candidate in (r"C:\Program Files\poppler-25.07.0\Library\bin",
                      r"C:\Program Files\poppler\Library\bin",
                      r"C:\poppler\Library\bin", r"C:\poppler\bin"):
        if Path(candidate).exists():
            return candidate
    return None


def read_image(path: Path) -> Extracted:
    """OCR a single image. Returns an Extracted whose `ocr_pages` is 1."""
    from PIL import Image

    return Extracted(text=_clean(ocr_image(Image.open(path))), pages=1,
                     kind="image", ocr_pages=1)


def table_row(number: int, cells: list[str]) -> str:
    """One table row, in the single format every reader emits and the splitter understands.

    `[row 4] A-1 | Banks shall ... | 01-Oct`

    All three readers that meet tables — Word, Excel and PowerPoint — go through here, and
    that is the point. `segment.py` splits on the `[row N]` marker, so a reader that emits
    a bare `A | B | C` line instead produces rows the splitter cannot see: they are not
    made into clauses, and worse, they are GLUED ONTO THE END of whatever clause came
    before, corrupting its text and its retrieval. That is what Word did until this
    function existed, silently, on every circular with a table in it.

    `number` is the sheet row for a spreadsheet, where it is a real coordinate a reviewer
    can navigate to. For Word and PowerPoint it is a running count of rows in the
    document, which is only a locator — but `segment._row_ref` prefers the row's own
    reference from its first cell whenever there is one, so this rarely surfaces.
    """
    return f"[row {number}] " + " | ".join(cells)


def read_docx(path: Path) -> Extracted:
    """Read a Word file: paragraphs AND table rows.

    Tables matter — obligations in SBP circulars are routinely inside them, and a reader
    that walks only paragraphs loses those clauses without any error.
    """
    from docx import Document

    document = Document(str(path))
    parts = [p.text for p in document.paragraphs if p.text.strip()]

    # Tables carry obligations routinely — a row is often one requirement. Keep the row
    # structure with a separator rather than flattening it into prose, and tag each row
    # with the SAME `[row N]` marker the spreadsheet reader uses (see `table_row`).
    row_number = 0
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                row_number += 1
                parts.append(table_row(row_number, cells))

    result = Extracted(text=_clean("\n".join(parts)), pages=1, kind="docx")
    result.children = _embedded_in_ooxml(path)
    return result


def read_excel(path: Path) -> Extracted:
    """Read every sheet and row, keeping the row number with the text.

    The `[row 14]` marker travels with the clause so a reviewer can find it again, and
    segment.py uses it as a clause boundary — one requirement per row.
    """
    from openpyxl import load_workbook

    wb = load_workbook(str(path), read_only=True, data_only=True)
    blocks = []
    for name in wb.sheetnames:
        sheet = wb[name]
        blocks.append(f"### SHEET: {name}")
        for number, row in enumerate(sheet.iter_rows(values_only=True), start=1):
            cells = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cells:
                # The row reference travels with the text so a reviewer can find it
                # again — "Annexure A · row 14" rather than "somewhere in the sheet".
                blocks.append(table_row(number, cells))
    return Extracted(text=_clean("\n".join(blocks)), pages=len(wb.sheetnames),
                     kind="excel")


def _pptx_shape_text(shape, out: list, counter: list) -> None:
    """Append every piece of text a slide shape carries. Recurses into groups.

    Three shape kinds hold text and they are NOT interchangeable:

    * a text box or placeholder — `has_text_frame`
    * a TABLE — no text frame at all, so a `has_text_frame` check drops it in silence.
      Obligations arrive in tables constantly, exactly as they do in Word, and a briefing
      deck's control matrix is usually the only part worth reading.
    * a GROUP — shapes dragged together. Its children are not in `slide.shapes`, so
      anything grouped is invisible unless you descend into it.

    Missing any of these loses clauses with no error, which is worse than failing to read
    the file at all: the deck reports as ingested and the obligations are simply absent.
    """
    if shape.shape_type == 6:                       # MSO_SHAPE_TYPE.GROUP
        for child in shape.shapes:
            _pptx_shape_text(child, out, counter)
        return
    if getattr(shape, "has_table", False):
        for row in shape.table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                # Through the shared helper, so a slide table row reaches the splitter as
                # a row and not as text glued to the previous clause.
                counter[0] += 1
                out.append(table_row(counter[0], cells))
        return
    if shape.has_text_frame and shape.text_frame.text.strip():
        out.append(shape.text_frame.text)


def read_pptx(path: Path) -> Extracted:
    """Slide text, slide tables and speaker notes. Returns an error result if python-pptx
    is absent, rather than raising — an optional dependency must not fail the batch.

    Text drawn INTO a slide as a picture is not read: this is a text extractor, not OCR.
    A deck that is entirely screenshots comes back nearly empty and is flagged by the
    split check rather than passing as a document with no obligations in it.
    """
    try:
        from pptx import Presentation
    except ImportError:
        return Extracted(error="python-pptx is not installed — .pptx cannot be read",
                         kind="pptx")
    prs = Presentation(str(path))
    parts = []
    counter = [0]                       # running table-row number across the whole deck
    for index, slide in enumerate(prs.slides, start=1):
        parts.append(f"### SLIDE {index}")
        for shape in slide.shapes:
            _pptx_shape_text(shape, parts, counter)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame.text.strip():
            # Speaker notes carry the instruction the slide only summarises often enough
            # to be worth reading. Labelled, so a reviewer can see where it came from.
            parts.append("Notes: " + slide.notes_slide.notes_text_frame.text)
    return Extracted(text=_clean("\n".join(parts)), pages=len(prs.slides), kind="pptx")


def read_msg(path: Path) -> Extracted:
    """Read an Outlook .msg: header, body and attachments. Attachments become children.

    Returns an error result if extract-msg is absent, rather than raising.
    """
    try:
        import extract_msg
    except ImportError:
        return Extracted(error="extract-msg is not installed — Outlook .msg cannot be read",
                         kind="msg")
    message = extract_msg.Message(str(path))
    header = f"Subject: {message.subject}\nFrom: {message.sender}\nDate: {message.date}"
    result = Extracted(text=_clean(header + "\n\n" + (message.body or "")), pages=1,
                       kind="msg")
    for attachment in message.attachments:
        name = attachment.longFilename or attachment.shortFilename or "attachment"
        if Path(name).suffix.lower() in SUPPORTED:
            with tempfile.TemporaryDirectory() as tmp:
                saved = Path(tmp) / name
                saved.write_bytes(attachment.data)
                result.children.append((name, read(saved, _depth=1)))
    return result


def read_text(path: Path) -> Extracted:
    """Read text, HTML, RTF or CSV, decoding with a fallback so an unusual encoding
    produces text rather than an exception.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() in {".htm", ".html"}:
        raw = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.S | re.I)
        raw = re.sub(r"<[^>]+>", " ", raw)
    if path.suffix.lower() == ".rtf":
        raw = re.sub(r"\\[a-z]+-?\d*\s?|[{}]", " ", raw)
    return Extracted(text=_clean(raw), pages=1, kind=path.suffix.lstrip("."))


def read_eml(path: Path) -> Extracted:
    """Read an email: body plus attachments, each attachment extracted as a child document.

    ABL confirmed RIA content arrives either in the body or attached, so both are read
    and the route is recorded on the document.
    """
    import email

    message = email.message_from_bytes(path.read_bytes())
    body = []
    result = Extracted(pages=1, kind="eml")
    for part in message.walk():
        if part.get_content_type() == "text/plain" and not part.get_filename():
            payload = part.get_payload(decode=True)
            if payload:
                body.append(payload.decode("utf-8", "replace"))
        name = part.get_filename()
        if name and Path(name).suffix.lower() in SUPPORTED:
            with tempfile.TemporaryDirectory() as tmp:
                saved = Path(tmp) / name
                saved.write_bytes(part.get_payload(decode=True) or b"")
                result.children.append((name, read(saved, _depth=1)))
    header = f"Subject: {message.get('Subject', '')}\nFrom: {message.get('From', '')}"
    result.text = _clean(header + "\n\n" + "\n".join(body))
    return result


def read_legacy(path: Path) -> Extracted:
    """.doc / .xls / .ppt — convert with LibreOffice, then read normally."""
    soffice = find_soffice()
    if soffice is None:
        return Extracted(error="LibreOffice is not installed — legacy "
                               f"{path.suffix} cannot be converted", kind="legacy")
    target = LEGACY[path.suffix.lower()]
    with tempfile.TemporaryDirectory() as tmp:
        try:
            subprocess.run(
                [str(soffice), "--headless", "--convert-to", target.lstrip("."),
                 "--outdir", tmp, str(path)],
                capture_output=True, timeout=180, check=False)
        except Exception as exc:
            return Extracted(error=f"LibreOffice conversion failed: {exc}", kind="legacy")
        converted = list(Path(tmp).glob("*" + target))
        if not converted:
            return Extracted(error="LibreOffice produced no output — the file may be "
                                   "password-protected or corrupt", kind="legacy")
        result = read(converted[0], _depth=1)
        result.kind = f"legacy{path.suffix}"
        return result


READERS = {
    ".pdf": read_pdf, ".docx": read_docx,
    ".xlsx": read_excel, ".xlsm": read_excel,
    ".pptx": read_pptx, ".msg": read_msg, ".eml": read_eml,
    ".doc": read_legacy, ".xls": read_legacy, ".ppt": read_legacy,
    ".txt": read_text, ".csv": read_text, ".htm": read_text,
    ".html": read_text, ".rtf": read_text,
    **{suffix: read_image for suffix in IMAGES},
}


# ====== EMBEDDED OBJECTS ======

# An OLE2 compound file. Word writes one of these — "oleObject1.bin" — whenever IT
# saves an embedded object, which is to say for every document that came from a real
# user. The workbook is not stored as a plain .xlsx; it is wrapped in this container.
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# What the payload turns out to be, judged by what is inside the zip rather than by any
# name — an OLE container carries no filename we can trust.
_ZIP_MARKERS = [
    ("xl/workbook.xml", ".xlsx"),
    ("word/document.xml", ".docx"),
    ("ppt/presentation.xml", ".pptx"),
]


def _unwrap_ole(data: bytes) -> tuple[str, bytes] | None:
    """Pull the real file out of an OLE2 wrapper. Returns (suffix, bytes) or None.

    Modern Office payloads (xlsx/docx/pptx) are zips stored whole inside the container,
    so the zip can be carved out by its own signatures — no extra dependency, which
    matters for an air-gapped install. olefile is used if it happens to be present,
    for legacy .xls payloads that are not zips.
    """
    if not data.startswith(_OLE_MAGIC):
        return None

    start = data.find(b"PK\x03\x04")
    end = data.rfind(b"PK\x05\x06")
    if start != -1 and end > start:
        payload = data[start:end + 22]                  # +22 = end-of-central-directory
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as inner:
                names = set(inner.namelist())
            for marker, suffix in _ZIP_MARKERS:
                if marker in names:
                    return suffix, payload
        except Exception:
            pass                                        # carve failed — try olefile

    try:
        import olefile
    except ImportError:
        return None
    try:
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            streams = {"/".join(s).lower(): s for s in ole.listdir()}
            for key, suffix in (("package", None), ("workbook", ".xls"), ("book", ".xls")):
                if key in streams:
                    payload = ole.openstream(streams[key]).read()
                    if suffix:
                        return suffix, payload
                    return (_sniff_bytes(payload) or ".xlsx"), payload
    except Exception:
        pass
    return None


def _sniff_bytes(data: bytes) -> str:
    """Suffix for a payload we hold in memory, decided by content."""
    if data[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = set(z.namelist())
            for marker, suffix in _ZIP_MARKERS:
                if marker in names:
                    return suffix
        except Exception:
            return ""
    return ""


def _embedded_in_ooxml(path: Path) -> list:
    """Documents stored inside a .docx / .xlsx package.

    ABL's own BRD is the proof this matters: a .docx carrying an .xlsm and four .docx
    files under `word/embeddings/`, and the embedded workbook is the specification.
    Reading the outer file and stopping loses it, silently.

    Two shapes appear in the wild and both are handled: the payload stored under its own
    name (Word 2016+ sometimes does this), and the payload wrapped in an OLE2 container
    called oleObject1.bin (what Word writes on save). Only the first was handled
    originally, so an annexure vanished the moment anyone opened and re-saved the
    circular in Word.
    """
    found = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist()
                     if "/embeddings/" in n
                     and Path(n).suffix.lower() in SUPPORTED | {".bin"}]
            for name in names[:12]:                     # a sane cap for a demo
                data = archive.read(name)
                label = Path(name).name

                if Path(name).suffix.lower() == ".bin":
                    unwrapped = _unwrap_ole(data)
                    if not unwrapped:
                        # Recorded, never dropped — an annexure that disappears quietly
                        # is worse than one that reports it could not be opened.
                        found.append((label, Extracted(
                            kind="ole-object",
                            error="embedded object could not be unwrapped from its OLE "
                                  "container")))
                        continue
                    suffix, data = unwrapped
                    label = Path(name).stem + suffix

                with tempfile.TemporaryDirectory() as tmp:
                    saved = Path(tmp) / label
                    saved.write_bytes(data)
                    found.append((label, read(saved, _depth=1)))
    except Exception:
        pass                                            # not a zip, or unreadable
    return found


# ====== ENTRY POINT ======

def read(path: Path, _depth: int = 0, _seen: set | None = None) -> Extracted:
    """Read one file. Never raises — a failure comes back in `.error`."""
    path = Path(path)
    _seen = _seen if _seen is not None else set()

    if _depth > MAX_DEPTH:
        return Extracted(error=f"nesting deeper than {MAX_DEPTH} levels — not followed")

    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception as exc:
        return Extracted(error=f"could not be opened: {exc}")

    if digest in _seen:                                 # a container referencing itself
        return Extracted(error="already extracted in this run — not followed again")
    _seen.add(digest)

    if path.stat().st_size == 0:
        return Extracted(error="the file is empty")

    suffix = sniff(path)
    reader = READERS.get(suffix)
    if reader is None:
        return Extracted(error=f"unsupported file type {suffix!r}", kind=suffix)

    try:
        result = reader(path)
    except Exception as exc:
        message = str(exc)
        if "password" in message.lower() or "encrypt" in message.lower():
            message = "the file is password-protected"
        return Extracted(error=f"could not be read: {message}", kind=suffix)

    if len(result.text) > MAX_TOTAL_CHARS:
        result.text = result.text[:MAX_TOTAL_CHARS]
        result.error = (result.error + " | " if result.error else "") + \
            f"truncated at {MAX_TOTAL_CHARS:,} characters"

    # ===== DEBUG PRINTS — delete this block when you are done =====
    _flat = result.text.replace("\n", " / ")
    print(f"[EXTRACT] {path.name}")
    print(f"[EXTRACT]   ext {path.suffix.lower()} -> sniffed {suffix}   kind={result.kind}")
    print(f"[EXTRACT]   pages={result.pages}  chars={len(result.text):,}  "
          f"ocr_pages={result.ocr_pages}  error={result.error or '-'}")
    # Head AND tail. A reader that stopped early looks perfectly fine from the head.
    print(f"[EXTRACT]   head: {_flat[:200]}")
    print(f"[EXTRACT]   tail: {_flat[-200:]}")
    for _name, _child in result.children:
        print(f"[EXTRACT]   embedded: {_name} -> {_child.kind}, "
              f"{len(_child.text):,} chars, error={_child.error or '-'}")
    # ===== END DEBUG PRINTS =====

    return result


# ====== TITLE AND DATE ======

_DATE = re.compile(
    r"\b(\d{1,2}[-/ ][A-Z][a-z]{2,8}[-/ ]\d{2,4}|[A-Z][a-z]{2,8} \d{1,2}, \d{4}|\d{4}-\d{2}-\d{2})\b"
)


def guess_title(text: str, filename: str) -> str:
    """A human-readable title from the first lines, falling back to the filename.

    This is what the UI shows, and it matters most for EMBEDDED files: Word names them
    `oleObject1.bin`, so without a detected title a reviewer cannot tell one annexure
    from another.
    """
    for line in (text or "").split("\n")[:25]:
        # A spreadsheet's first line arrives as "[row 1] ANNEXURE A — ...". The row
        # marker belongs in the clause text and is noise in a title.
        line = re.sub(r"^\[row \d+\]\s*", "", line.strip())
        lowered = line.lower()
        if any(w in lowered for w in ("confidential", "for bank", "internal use",
                                      "page ", "table of contents", "all rights",
                                      "### sheet", "### slide")):
            continue
        if 12 <= len(line) <= 110:
            letters = [c for c in line if c.isalpha()]
            if letters and sum(c.isupper() for c in letters) / len(letters) > 0.4:
                return line
    return Path(filename).stem.replace("_", " ").strip()


def guess_date(text: str) -> str:
    """The first date that looks like a circular date, or "". Searched near the top only,
    so a date inside the body cannot be mistaken for the document's own.
    """
    match = _DATE.search((text or "")[:2500])
    return match.group(1) if match else ""
