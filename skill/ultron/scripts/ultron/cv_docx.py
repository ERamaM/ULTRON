"""Bounded text extraction from Word ``.docx`` CVs with the standard library only.

A ``.docx`` file is a zip archive whose body is ``word/document.xml``. Only that part is read: headers
and footers (often contact details) are left out. The archive and the XML are untrusted input, so:

- the archive may hold at most ``MAX_ARCHIVE_ENTRIES`` entries, and the body is streamed with a hard cap
  of ``MAX_DOCUMENT_XML_BYTES`` inflated bytes (a declared size can lie, so the read itself is bounded);
- the body must be UTF-8 (as Word writes it), and any document type declaration is rejected before
  parsing (and again by the parser's handlers), so no entity is ever defined or expanded and nothing
  outside the archive is fetched;
- the body is parsed as a stream (expat handlers, no tree), with at most ``MAX_XML_ELEMENTS`` elements
  and ``MAX_XML_DEPTH`` levels of nesting;
- no single tag may exceed ``MAX_XML_TAG_BYTES`` (checked before parsing, quotes included), because the
  parser materializes all of a tag's attributes before any handler can count them.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from xml.parsers import expat

MAX_ARCHIVE_ENTRIES = 2_000
MAX_DOCUMENT_XML_BYTES = 25_000_000
MAX_XML_ELEMENTS = 300_000  # a long CV has a few tens of thousands
MAX_XML_DEPTH = 128
MAX_RAW_TEXT_CHARS = 60_000 * 8 + 65_536  # the PDF worker raw-output budget at the default CV limit
MAX_XML_TAG_BYTES = 64 * 1024  # a whole markup tag with its attributes; Word writes a few hundred bytes
_CHUNK = 64 * 1024
_ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_BODY = "word/document.xml"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main "
_MC_FALLBACK = "http://schemas.openxmlformats.org/markup-compatibility/2006 Fallback"
_W_P, _W_T, _W_TAB, _W_TYPE = f"{_W}p", f"{_W}t", f"{_W}tab", f"{_W}type"
_W_NO_BREAK_HYPHEN = f"{_W}noBreakHyphen"
_W_BREAKS = frozenset({f"{_W}br", f"{_W}cr"})
_DOCTYPE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
# Characters that open or close a markup tag or a quoted attribute value.
_TAG_TOKENS = re.compile(rb"[<>\"']")


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
        # LZMA and BZIP2 members buffer their whole output before read() returns, so the streamed cap could not
        # bound memory; Word writes stored or deflated parts only.
        if info.flag_bits & 0x1:  # an encrypted member: zipfile would raise RuntimeError asking for a password
            raise _error("The Word document body is encrypted")
        if info.compress_type not in _ALLOWED_COMPRESSION:
            raise _error("The Word document body uses an unsupported compression method")
        data = bytearray()
        try:
            with archive.open(info) as stream:
                while chunk := stream.read(_CHUNK):
                    data += chunk
                    if len(data) > MAX_DOCUMENT_XML_BYTES:
                        raise _error("The Word document body is too large", code="too_long")
        except (zipfile.BadZipFile, zlib.error, OSError, ValueError, EOFError, NotImplementedError, RuntimeError) as exc:
            raise _error("The Word document body cannot be read") from exc
    return bytes(data)


class _BodyText:
    """Streaming expat handlers: no tree is built, so memory stays bounded by the nesting depth."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars  # raw text budget, enforced while parsing (before any normalization)
        self.chars = 0
        self.lines: list[str] = []
        self.stack: list[list[str]] = []  # one text buffer per open w:p (text boxes nest paragraphs)
        self.loose: list[str] = []  # text outside any paragraph
        self.pages = 1
        self.elements = 0
        self.depth = 0
        self.skipped = 0  # depth inside mc:Fallback (the same content as its mc:Choice)
        self.in_text = 0

    def _buffer(self) -> list[str]:
        return self.stack[-1] if self.stack else self.loose

    def _count(self, size: int) -> None:
        self.chars += size
        if self.chars > self.max_chars:
            raise _error("The Word document body has too much text", code="too_long")

    def _append(self, text: str) -> None:
        self._count(len(text))
        self._buffer().append(text)

    def _line(self, parts: list[str]) -> None:
        self._count(1)  # the line separator
        self.lines.append("".join(parts))

    def start(self, name: str, attributes: dict[str, str]) -> None:
        self.elements += 1
        self.depth += 1
        if self.elements > MAX_XML_ELEMENTS or self.depth > MAX_XML_DEPTH:
            raise _error("The Word document body has too many elements", code="too_long")
        if name == _MC_FALLBACK:
            self.skipped += 1
        if self.skipped:
            return
        if name == _W_P:
            # A paragraph nested in another (a text box) ends the outer paragraph's text so far.
            if self.stack and self.stack[-1]:
                self._line(self.stack[-1])
                self.stack[-1] = []
            self.stack.append([])
        elif name == _W_T:
            self.in_text += 1
        elif name == _W_TAB:
            self._append("\t")
        elif name == _W_NO_BREAK_HYPHEN:
            self._append("-")
        elif name in _W_BREAKS:
            if attributes.get(_W_TYPE) == "page":
                self.pages += 1
            # Every break (line, column or page) separates the text on its two sides.
            self._append("\n")

    def end(self, name: str) -> None:
        self.depth -= 1
        if name == _MC_FALLBACK:
            self.skipped -= 1
            return
        if self.skipped:
            return
        if name == _W_P and self.stack:
            self._line(self.stack.pop())
        elif name == _W_T:
            self.in_text -= 1

    def characters(self, data: str) -> None:
        if self.in_text and not self.skipped:
            self._append(data)


def _reject_declaration(*_args) -> None:
    raise _error("The Word document declares a document type, which is not supported")


def _check_tag_sizes(body: bytes) -> None:
    """Reject a tag longer than ``MAX_XML_TAG_BYTES``; a quoted ``>`` does not end a tag.

    A linear scan over the few significant characters, so a crafted tag cannot make the check itself costly.
    """
    start = -1  # offset of the open tag's "<", or -1 outside markup
    quote = 0
    for match in _TAG_TOKENS.finditer(body):
        position, char = match.start(), body[match.start()]
        if start < 0:
            if char == 0x3C:  # "<"
                start = position
            continue
        if position - start > MAX_XML_TAG_BYTES:
            break
        if quote:
            if char == quote:
                quote = 0
        elif char in (0x22, 0x27):  # '"' or "'"
            quote = char
        elif char == 0x3E:  # ">"
            start = -1
    if start >= 0 and len(body) - start > MAX_XML_TAG_BYTES:
        raise _error("The Word document body has an oversized tag", code="too_long")


def _text(body: bytes, max_chars: int = MAX_RAW_TEXT_CHARS) -> tuple[str, int]:
    """Paragraph text and the page count (explicit page breaks plus one)."""
    # Word writes the body as UTF-8. A UTF-16 body (NUL bytes) could hide a declaration from the byte check.
    if b"\x00" in body or body.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise _error("The Word document body is not UTF-8")
    if _DOCTYPE.search(body):
        _reject_declaration()
    _check_tag_sizes(body)
    handlers = _BodyText(max_chars)
    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator=" ")
    parser.StartElementHandler = handlers.start
    parser.EndElementHandler = handlers.end
    parser.CharacterDataHandler = handlers.characters
    # Defence in depth behind the byte check: any declaration stops the parse.
    parser.StartDoctypeDeclHandler = _reject_declaration
    parser.EntityDeclHandler = _reject_declaration
    parser.ExternalEntityRefHandler = _reject_declaration
    try:
        for offset in range(0, len(body), _CHUNK):
            parser.Parse(body[offset : offset + _CHUNK], False)
        parser.Parse(b"", True)
    except expat.ExpatError as exc:
        raise _error("The Word document body is not valid XML") from exc
    lines = handlers.lines + (["".join(handlers.loose)] if handlers.loose else [])
    return "\n".join(lines), handlers.pages

def extract_docx_text(content: bytes, max_chars: int = MAX_RAW_TEXT_CHARS) -> tuple[str, int]:
    """Return the raw body text of a ``.docx`` file and its page count, or raise a CV extraction error.

    ``max_chars`` bounds the raw text (separators included) while it is parsed, so a crafted body cannot
    build an unbounded string or line list before the CV character budget is checked.
    """
    return _text(_read_body(content), max_chars)
