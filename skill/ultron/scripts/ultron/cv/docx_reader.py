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
from dataclasses import dataclass, field
from xml.parsers import expat

MAX_ARCHIVE_ENTRIES = 2_000
MAX_DOCUMENT_XML_BYTES = 25_000_000
MAX_AUX_XML_BYTES = 1_000_000
MAX_HEADER_FOOTER_PARTS = 16
MAX_FONTS = 30
MAX_XML_ELEMENTS = 300_000  # a long CV has a few tens of thousands
MAX_XML_DEPTH = 128
MAX_RAW_TEXT_CHARS = 60_000 * 8 + 65_536  # the PDF worker raw-output budget at the default CV limit
MAX_XML_TAG_BYTES = 64 * 1024  # a whole markup tag with its attributes; Word writes a few hundred bytes
_CHUNK = 64 * 1024
_ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_BODY = "word/document.xml"
_STYLES = "word/styles.xml"
_COMMENTS = "word/comments.xml"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main "
_A = "http://schemas.openxmlformats.org/drawingml/2006/main "
_V = "urn:schemas-microsoft-com:vml "
_MC_FALLBACK = "http://schemas.openxmlformats.org/markup-compatibility/2006 Fallback"
_W_P, _W_T, _W_TAB, _W_TYPE = f"{_W}p", f"{_W}t", f"{_W}tab", f"{_W}type"
_W_NO_BREAK_HYPHEN = f"{_W}noBreakHyphen"
_W_BREAKS = frozenset({f"{_W}br", f"{_W}cr"})
_W_TBL = f"{_W}tbl"
_W_TXBX = f"{_W}txbxContent"
_V_TEXTBOX = f"{_V}textbox"
_W_COLS = f"{_W}cols"
_W_DEL, _W_INS, _W_VANISH = f"{_W}del", f"{_W}ins", f"{_W}vanish"
_W_NUMPR = f"{_W}numPr"
_W_RFONTS = f"{_W}rFonts"
_A_BLIP = f"{_A}blip"
_V_IMAGEDATA = f"{_V}imagedata"
_HEADER_FOOTER_RE = re.compile(r"^word/(?:header|footer)\d*\.xml$", re.IGNORECASE)
_CONTACT_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,6}[A-Za-z]{2,24}\b|"
    r"(?:https?://|linkedin\.com/|github\.com/)\S+|"
    r"(?<![\d$€£¥₹])(?:\+\d{1,3}(?:[\s.-]?\(?\d{2,4}\)?){2,4}|\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]\d{3,4}|\d{3,4}[\s-]\d{3,4}[\s-]\d{3,4})(?!\d)",
    re.IGNORECASE,
)
_ZERO_WIDTH_CHARS = frozenset("\u200b\u200c\u200d\u2060\ufeff\u00ad")
_DOCTYPE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
# Characters that open or close a markup tag or a quoted attribute value.
_TAG_TOKENS = re.compile(rb"[<>\"']")


@dataclass(frozen=True, slots=True)
class DocxStructureReport:
    """Extracted text, page count, and structural layout metrics for a ``.docx`` file."""
    text: str
    pages: int
    table_count: int = 0
    textbox_count: int = 0
    multi_column_sections: int = 0
    image_count: int = 0
    has_tracked_changes: bool = False
    has_comments: bool = False
    has_hidden_text: bool = False
    list_paragraph_count: int = 0
    fonts: list[str] = field(default_factory=list)
    non_standard_char_count: int = 0
    header_footer_parts: int = 0
    header_footer_text_chars: int = 0
    header_footer_has_contact: bool = False


def _error(message: str, code: str = "no_extractable_text"):
    from ultron.cv.extract import CvExtractionError

    return CvExtractionError(message, code=code)


def _open_archive(content: bytes) -> tuple[zipfile.ZipFile, list[zipfile.ZipInfo]]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise _error("The file is not a readable Word document") from exc
    entries = archive.infolist()
    if len(entries) > MAX_ARCHIVE_ENTRIES:
        archive.close()
        raise _error("The Word document has too many parts", code="too_long")
    return archive, entries


def _read_member(archive: zipfile.ZipFile, name: str, max_bytes: int) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as exc:
        raise _error("The Word document has no body") from exc
    if info.file_size > max_bytes:
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
                if len(data) > max_bytes:
                    raise _error("The Word document body is too large", code="too_long")
    except (zipfile.BadZipFile, zlib.error, OSError, ValueError, EOFError, NotImplementedError, RuntimeError) as exc:
        raise _error("The Word document body cannot be read") from exc
    return bytes(data)


def _read_body(content: bytes) -> bytes:
    archive, _entries = _open_archive(content)
    with archive:
        return _read_member(archive, _BODY, MAX_DOCUMENT_XML_BYTES)


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
        self.table_count = 0
        self.textbox_count = 0
        self.multi_column_sections = 0
        self.image_refs = 0
        self.has_tracked_changes = False
        self.has_hidden_text = False
        self.list_paragraph_count = 0
        self.fonts: list[str] = []
        self._seen_fonts: set[str] = set()
        self.non_standard_char_count = 0

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

    def _record_structure(self, name: str, attributes: dict[str, str]) -> None:
        if name == _W_TBL:
            self.table_count += 1
        elif name in {_W_TXBX, _V_TEXTBOX}:
            self.textbox_count += 1
        elif name == _W_COLS:
            raw_num = attributes.get(f"{_W}num") or attributes.get("num") or ""
            if raw_num.isdigit() and int(raw_num) > 1:
                self.multi_column_sections += 1
        elif name in {_A_BLIP, _V_IMAGEDATA}:
            self.image_refs += 1
        elif name in {_W_DEL, _W_INS}:
            self.has_tracked_changes = True
        elif name == _W_VANISH:
            self.has_hidden_text = True
        elif name == _W_NUMPR:
            self.list_paragraph_count += 1
        elif name == _W_RFONTS and len(self.fonts) < MAX_FONTS:
            for attr in (f"{_W}ascii", f"{_W}hAnsi", f"{_W}cs", f"{_W}eastAsia", "ascii", "hAnsi"):
                font = (attributes.get(attr) or "").strip()
                if font and font not in self._seen_fonts:
                    self._seen_fonts.add(font)
                    self.fonts.append(font)
                    if len(self.fonts) >= MAX_FONTS:
                        break

    def start(self, name: str, attributes: dict[str, str]) -> None:
        self.elements += 1
        self.depth += 1
        if self.elements > MAX_XML_ELEMENTS or self.depth > MAX_XML_DEPTH:
            raise _error("The Word document body has too many elements", code="too_long")
        if name == _MC_FALLBACK:
            self.skipped += 1
        if self.skipped:
            return
        self._record_structure(name, attributes)
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
            for ch in data:
                code = ord(ch)
                if 0xE000 <= code <= 0xF8FF or 0xFB00 <= code <= 0xFB06 or ch in _ZERO_WIDTH_CHARS:
                    self.non_standard_char_count += 1
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


def _parse_xml_body(body: bytes, max_chars: int = MAX_RAW_TEXT_CHARS) -> tuple[str, _BodyText]:
    """Parse one OOXML part with bounded streaming expat handlers."""
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
    return "\n".join(lines), handlers


def _text(body: bytes, max_chars: int = MAX_RAW_TEXT_CHARS) -> tuple[str, int]:
    """Paragraph text and the page count (explicit page breaks plus one)."""
    text, handlers = _parse_xml_body(body, max_chars)
    return text, handlers.pages


def extract_docx_text(content: bytes, max_chars: int = MAX_RAW_TEXT_CHARS) -> tuple[str, int]:
    """Return the raw body text of a ``.docx`` file and its page count, or raise a CV extraction error.

    ``max_chars`` bounds the raw text (separators included) while it is parsed, so a crafted body cannot
    build an unbounded string or line list before the CV character budget is checked.
    """
    return _text(_read_body(content), max_chars)


def inspect_docx(content: bytes, max_chars: int = MAX_RAW_TEXT_CHARS) -> DocxStructureReport:
    """Extract raw body text plus bounded ATS structural indicators from a ``.docx`` archive."""
    archive, entries = _open_archive(content)
    with archive:
        body_bytes = _read_member(archive, _BODY, MAX_DOCUMENT_XML_BYTES)
        text, body_handlers = _parse_xml_body(body_bytes, max_chars)

        entry_names = [entry.filename for entry in entries]
        media_entries = sum(1 for name in entry_names if name.lower().startswith("word/media/") and not name.endswith("/"))
        has_comments = any(name.lower() == _COMMENTS for name in entry_names)
        hf_names = sorted(name for name in entry_names if _HEADER_FOOTER_RE.match(name))[:MAX_HEADER_FOOTER_PARTS]

        fonts = list(body_handlers.fonts)
        if _STYLES in entry_names and len(fonts) < MAX_FONTS:
            styles_bytes = _read_member(archive, _STYLES, MAX_AUX_XML_BYTES)
            _styles_text, style_handlers = _parse_xml_body(styles_bytes, MAX_AUX_XML_BYTES)
            for font in style_handlers.fonts:
                if font not in fonts:
                    fonts.append(font)
                    if len(fonts) >= MAX_FONTS:
                        break

        hf_chars = 0
        hf_has_contact = False
        for hf_name in hf_names:
            hf_bytes = _read_member(archive, hf_name, MAX_AUX_XML_BYTES)
            hf_text, _ = _parse_xml_body(hf_bytes, MAX_AUX_XML_BYTES)
            stripped = hf_text.strip()
            hf_chars += len(stripped)
            if stripped and _CONTACT_RE.search(stripped):
                hf_has_contact = True

    return DocxStructureReport(
        text=text,
        pages=body_handlers.pages,
        table_count=body_handlers.table_count,
        textbox_count=body_handlers.textbox_count,
        multi_column_sections=body_handlers.multi_column_sections,
        image_count=max(media_entries, body_handlers.image_refs),
        has_tracked_changes=body_handlers.has_tracked_changes,
        has_comments=has_comments,
        has_hidden_text=body_handlers.has_hidden_text,
        list_paragraph_count=body_handlers.list_paragraph_count,
        fonts=fonts,
        non_standard_char_count=body_handlers.non_standard_char_count,
        header_footer_parts=len(hf_names),
        header_footer_text_chars=hf_chars,
        header_footer_has_contact=hf_has_contact,
    )
