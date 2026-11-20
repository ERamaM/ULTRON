"""Bounded text extraction from Word ``.docx`` CVs with the standard library only.

A ``.docx`` file is a zip archive whose body is ``word/document.xml``. Only that part is read: headers
and footers (often contact details) are left out. The archive and the XML are untrusted input, so:

- the archive may hold at most ``MAX_ARCHIVE_ENTRIES`` entries, and the body is streamed with a hard cap
  of ``MAX_DOCUMENT_XML_BYTES`` inflated bytes (a declared size can lie, so the read itself is bounded);
- the body must be UTF-8 (as Word writes it), and any document type declaration is rejected before
  parsing, so no entity is ever defined or expanded and nothing outside the archive is fetched.
"""

from __future__ import annotations

import io
import re
import zipfile
from xml.etree import ElementTree

MAX_ARCHIVE_ENTRIES = 2_000
MAX_DOCUMENT_XML_BYTES = 25_000_000
_CHUNK = 64 * 1024
_BODY = "word/document.xml"
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
_DOCTYPE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)


def _error(message: str, code: str = "no_extractable_text"):
    from ultron.cv import CvExtractionError

    return CvExtractionError(message, code=code)


def _read_body(content: bytes) -> bytes:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise _error("The file is not a readable Word document") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise _error("The Word document has too many parts", code="too_long")
        try:
            info = archive.getinfo(_BODY)
        except KeyError as exc:
            raise _error("The Word document has no body") from exc
        if info.file_size > MAX_DOCUMENT_XML_BYTES:
            raise _error("The Word document body is too large", code="too_long")
        data = bytearray()
        try:
            with archive.open(info) as stream:
                while chunk := stream.read(_CHUNK):
                    data += chunk
                    if len(data) > MAX_DOCUMENT_XML_BYTES:
                        raise _error("The Word document body is too large", code="too_long")
        except (zipfile.BadZipFile, OSError, ValueError, EOFError, NotImplementedError) as exc:
            raise _error("The Word document body cannot be read") from exc
    return bytes(data)


def _text(body: bytes) -> tuple[str, int]:
    """Paragraph text and the page count (explicit page breaks plus one)."""
    # Word writes the body as UTF-8. A UTF-16 body (NUL bytes) could hide a declaration from the byte check.
    if b"\x00" in body or body.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise _error("The Word document body is not UTF-8")
    if _DOCTYPE.search(body):
        raise _error("The Word document declares a document type, which is not supported")
    lines: list[str] = []
    current: list[str] = []
    pages = 1
    skipped = 0  # depth inside mc:Fallback (the same content as its mc:Choice)
    try:
        for event, element in ElementTree.iterparse(io.BytesIO(body), events=("start", "end")):
            tag = element.tag
            if tag == f"{_MC}Fallback":
                skipped += 1 if event == "start" else -1
                continue
            if skipped or event == "start":
                continue
            if tag == f"{_W}t" and element.text:
                current.append(element.text)
            elif tag == f"{_W}tab":
                current.append("\t")
            elif tag in {f"{_W}br", f"{_W}cr"}:
                if element.get(f"{_W}type") == "page":
                    pages += 1
                # Every break (line, column or page) separates the text on its two sides.
                current.append("\n")
            elif tag == f"{_W}p":
                lines.append("".join(current))
                current = []
            element.clear()
    except ElementTree.ParseError as exc:
        raise _error("The Word document body is not valid XML") from exc
    if current:
        lines.append("".join(current))
    return "\n".join(lines), pages


def extract_docx_text(content: bytes) -> tuple[str, int]:
    """Return the raw body text of a ``.docx`` file and its page count, or raise a CV extraction error."""
    return _text(_read_body(content))
