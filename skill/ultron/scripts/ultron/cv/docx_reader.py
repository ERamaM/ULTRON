"""Bounded text extraction from Word ``.docx`` CVs with the standard library only.

A DOCX file is a ZIP archive. Plain extraction reads body text and cached pagination. Structural
inspection also reads bounded styles, headers and footers. The archive and XML are untrusted, so:

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
from collections import Counter
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
_APP = "docProps/app.xml"
_APP_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties "
_APP_PAGES = f"{_APP_NS}Pages"
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
    explicit_page_breaks: int = 0
    rendered_page_breaks: int = 0
    app_pages: int | None = None
    heading_paragraph_count: int = 0
    page_width_twips: int | None = None
    page_height_twips: int | None = None
    margin_twips: int | None = None
    header_align: str = "left"
    accent_color: str = ""
    has_section_borders: bool = False
    has_shaded_headings: bool = False
    has_caps_headings: bool = False
    has_right_tabs: bool = False
    dominant_font: str = ""


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


def _read_app_pages(archive: zipfile.ZipFile) -> int | None:
    if _APP not in archive.namelist():
        return None
    _, props = _parse_xml_body(_read_member(archive, _APP, MAX_AUX_XML_BYTES), MAX_AUX_XML_BYTES)
    return props.app_pages


def _xml_page_count(body: _BodyText, app_pages: int | None) -> int:
    pages = 1 + max(body.explicit_page_breaks, body.rendered_page_breaks)
    return pages if body.rendered_page_breaks else max(pages, app_pages or 0)


class _BodyText:
    """Streaming expat handlers: no tree is built, so memory stays bounded by the nesting depth."""

    def __init__(self, max_chars: int) -> None:
        self.max_chars = max_chars  # raw text budget, enforced while parsing (before any normalization)
        self.chars = 0
        self.lines: list[str] = []
        self.stack: list[list[str]] = []  # one text buffer per open w:p (text boxes nest paragraphs)
        self.loose: list[str] = []  # text outside any paragraph
        self.explicit_page_breaks = 0
        self.rendered_page_breaks = 0
        self.heading_paragraph_count = 0
        self.page_width_twips: int | None = None
        self.page_height_twips: int | None = None
        self.margin_twips: int | None = None
        self.header_align = "left"
        self.accent_color = ""
        self.has_section_borders = False
        self.has_shaded_headings = False
        self.has_caps_headings = False
        self.has_right_tabs = False
        self._first_paragraph = False
        self._section_depth: int | None = None
        self._section_text: list[str] | None = None
        self.app_pages: int | None = None
        self._app_page_parts: list[str] | None = None
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
        self.font_frequency: Counter[str] = Counter()
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
        elif name == f"{_W}pStyle":
            style = attributes.get(f"{_W}val", "").casefold()
            if style in {"title", "subtitle"} or style.startswith("heading"):
                self.heading_paragraph_count += 1
        elif name == _W_NUMPR:
            self.list_paragraph_count += 1
        elif name == _W_RFONTS:
            self._record_fonts(attributes)

    def _record_fonts(self, attributes: dict[str, str]) -> None:
        seen: set[str] = set()
        for attr in (f"{_W}ascii", f"{_W}hAnsi", f"{_W}cs", f"{_W}eastAsia", "ascii", "hAnsi"):
            font = (attributes.get(attr) or "").strip()
            if not font or font in seen:
                continue
            seen.add(font)
            if font in self.font_frequency or len(self.fonts) < MAX_FONTS:
                if font not in self.font_frequency:
                    self.fonts.append(font)
                self.font_frequency[font] += 1

    def _record_layout(self, name: str, attributes: dict[str, str]) -> None:
        val = attributes.get(f"{_W}val", "")
        if name == f"{_W}pgSz" and self.page_width_twips is None:
            self.page_width_twips = _positive_twips(attributes.get(f"{_W}w", ""))
            self.page_height_twips = _positive_twips(attributes.get(f"{_W}h", ""))
        elif name == f"{_W}pgMar" and self.margin_twips is None:
            left = _positive_twips(attributes.get(f"{_W}left", ""))
            right = _positive_twips(attributes.get(f"{_W}right", ""))
            if left is not None and right is not None:
                self.margin_twips = (left + right) // 2
        elif name == f"{_W}jc" and self._first_paragraph and val == "center":
            self.header_align = "center"
        elif name == f"{_W}tab" and val == "right":
            self.has_right_tabs = True
        elif name == f"{_W}style" and attributes.get(f"{_W}styleId", "").casefold() == "heading2":
            self._section_depth = self.depth
        elif name == f"{_W}pStyle" and val.casefold() == "heading2":
            self._section_text = []
        elif self._section_depth is not None or self._section_text is not None:
            self._record_heading_layout(name, attributes)

    def _record_heading_layout(self, name: str, attributes: dict[str, str]) -> None:
        if name == f"{_W}pBdr":
            self.has_section_borders = True
        elif name == f"{_W}shd":
            self.has_shaded_headings = True
        elif name == f"{_W}caps":
            self.has_caps_headings = True
        color = attributes.get(f"{_W}val" if name == f"{_W}color" else f"{_W}color", "").upper()
        if not self.accent_color and re.fullmatch(r"[0-9A-F]{6}", color) and color not in {"000000", "FFFFFF"}:
            self.accent_color = color

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
        self._record_layout(name, attributes)
        if name == _W_P:
            self._first_paragraph = not self.lines and not self.stack
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
        elif name == f"{_W}lastRenderedPageBreak":
            self.rendered_page_breaks += 1
        elif name == _APP_PAGES and self.depth == 2:
            self._app_page_parts = []
        elif name in _W_BREAKS:
            if name == f"{_W}br" and attributes.get(_W_TYPE) == "page":
                self.explicit_page_breaks += 1
            # Every break (line, column or page) separates the text on its two sides.
            self._append("\n")

    def end(self, name: str) -> None:
        self.depth -= 1
        if name == _MC_FALLBACK:
            self.skipped -= 1
            return
        if self.skipped:
            return
        if name == _APP_PAGES and self._app_page_parts is not None:
            value = "".join(self._app_page_parts).strip()
            if value.isascii() and value.isdecimal() and len(value) <= 10:
                pages = int(value)
                if 0 < pages <= 2_147_483_647:
                    self.app_pages = pages
            self._app_page_parts = None
        if name == f"{_W}style" and self._section_depth == self.depth + 1:
            self._section_depth = None
        if name == _W_P and self.stack:
            self._first_paragraph = False
            if self._section_text is not None:
                self.has_caps_headings |= "".join(self._section_text).isupper()
                self._section_text = None
            self._line(self.stack.pop())
        elif name == _W_T:
            self.in_text -= 1

    def characters(self, data: str) -> None:
        if self._app_page_parts is not None and self.depth == 2:
            self._count(len(data))
            self._app_page_parts.append(data)
        if self.in_text and not self.skipped:
            if self._section_text is not None:
                self._section_text.append(data)
            for ch in data:
                code = ord(ch)
                if 0xE000 <= code <= 0xF8FF or 0xFB00 <= code <= 0xFB06 or ch in _ZERO_WIDTH_CHARS:
                    self.non_standard_char_count += 1
            self._append(data)


def _positive_twips(value: str) -> int | None:
    return int(value) if len(value) <= 6 and value.isascii() and value.isdecimal() else None


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


def extract_docx_text(content: bytes, max_chars: int = MAX_RAW_TEXT_CHARS) -> tuple[str, int]:
    """Return the raw body text of a ``.docx`` file and its page count, or raise a CV extraction error.

    ``max_chars`` bounds the raw text (separators included) while it is parsed, so a crafted body cannot
    build an unbounded string or line list before the CV character budget is checked.
    """
    archive, _ = _open_archive(content)
    with archive:
        text, body = _parse_xml_body(_read_member(archive, _BODY, MAX_DOCUMENT_XML_BYTES), max_chars)
        return text, _xml_page_count(body, _read_app_pages(archive))


def _dominant_font(body: _BodyText, styles: _BodyText) -> str:
    fonts = body.font_frequency or styles.font_frequency
    return fonts.most_common(1)[0][0] if fonts else ""


def inspect_docx(content: bytes, max_chars: int = MAX_RAW_TEXT_CHARS) -> DocxStructureReport:
    """Extract raw body text plus bounded ATS structural indicators from a ``.docx`` archive."""
    archive, entries = _open_archive(content)
    with archive:
        body_bytes = _read_member(archive, _BODY, MAX_DOCUMENT_XML_BYTES)
        text, body_handlers = _parse_xml_body(body_bytes, max_chars)

        app_pages = _read_app_pages(archive)
        entry_names = [entry.filename for entry in entries]
        media_entries = sum(1 for name in entry_names if name.lower().startswith("word/media/") and not name.endswith("/"))
        has_comments = any(name.lower() == _COMMENTS for name in entry_names)
        hf_names = sorted(name for name in entry_names if _HEADER_FOOTER_RE.match(name))[:MAX_HEADER_FOOTER_PARTS]

        style_handlers = _BodyText(0)
        fonts = list(body_handlers.fonts)
        if _STYLES in entry_names:
            styles_bytes = _read_member(archive, _STYLES, MAX_AUX_XML_BYTES)
            _styles_text, style_handlers = _parse_xml_body(styles_bytes, MAX_AUX_XML_BYTES)
            for font in style_handlers.fonts:
                if len(fonts) >= MAX_FONTS:
                    break
                if font not in fonts:
                    fonts.append(font)

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
        pages=_xml_page_count(body_handlers, app_pages),
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
        explicit_page_breaks=body_handlers.explicit_page_breaks,
        rendered_page_breaks=body_handlers.rendered_page_breaks,
        app_pages=app_pages,
        heading_paragraph_count=body_handlers.heading_paragraph_count,
        page_width_twips=body_handlers.page_width_twips,
        page_height_twips=body_handlers.page_height_twips,
        margin_twips=body_handlers.margin_twips,
        header_align=body_handlers.header_align,
        accent_color=body_handlers.accent_color or style_handlers.accent_color,
        has_section_borders=body_handlers.has_section_borders or style_handlers.has_section_borders,
        has_shaded_headings=body_handlers.has_shaded_headings or style_handlers.has_shaded_headings,
        has_caps_headings=body_handlers.has_caps_headings or style_handlers.has_caps_headings,
        has_right_tabs=body_handlers.has_right_tabs or style_handlers.has_right_tabs,
        dominant_font=_dominant_font(body_handlers, style_handlers),
    )
