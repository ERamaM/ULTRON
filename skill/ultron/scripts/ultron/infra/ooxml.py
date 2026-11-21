"""Shared pure-stdlib OOXML primitives for clean single-column Word (.docx) builders."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

WRITER_SAFE_FONTS: tuple[str, ...] = (
    "Calibri",
    "Arial",
    "Georgia",
    "Cambria",
    "Garamond",
    "Verdana",
    "Helvetica",
    "Times New Roman",
)
_SAFE_FONT_LOOKUP = {font.casefold(): font for font in WRITER_SAFE_FONTS}

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")
_MD_LINK_RE = re.compile(r"\[([^\[\]]+)\]\(([^()\s]+)\)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")


def resolve_safe_font(font: str) -> str:
    """Validate ``font`` against ``WRITER_SAFE_FONTS`` and return canonical casing."""
    key = " ".join(str(font or "").split()).casefold()
    if key not in _SAFE_FONT_LOOKUP:
        allowed = ", ".join(WRITER_SAFE_FONTS)
        raise ValueError(f"unsupported font {font!r}; choose an ATS-safe font ({allowed})")
    return _SAFE_FONT_LOOKUP[key]


def sanitize_xml_text(value: str) -> str:
    """Normalize line breaks and strip XML-illegal control/surrogate characters."""
    cleaned = str(value or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    return _SURROGATE_RE.sub("", cleaned)


def xml_escape(value: str) -> str:
    """Sanitize and XML-escape ``value`` including single and double quotes."""
    return escape(sanitize_xml_text(value), {'"': "&quot;", "'": "&apos;"})


def safe_hyperlink(url: str) -> str | None:
    """Allow only ``http(s)`` URLs with a host or ``mailto:`` addresses with ``@``."""
    candidate = sanitize_xml_text(url).strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"} and parsed.netloc:
        return candidate
    if scheme == "mailto" and "@" in parsed.path:
        return candidate
    return None


def run_xml(
    text: str,
    *,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
    size: int | None = None,
) -> str:
    """Render a single styled OOXML ``<w:r>`` text run."""
    props: list[str] = []
    if bold:
        props.append("<w:b/>")
    if italic:
        props.append("<w:i/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    if size:
        props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{xml_escape(text)}</w:t></w:r>'


def inline_runs_xml(
    text: str,
    rel_registry: list[tuple[str, str]],
    *,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
    size: int | None = None,
) -> str:
    """Render inline Markdown bold spans and validated hyperlinks into OOXML runs."""
    parts: list[str] = []

    def emit_plain(segment: str) -> None:
        pos = 0
        for bmatch in _BOLD_RE.finditer(segment):
            if bmatch.start() > pos:
                parts.append(run_xml(segment[pos : bmatch.start()], bold=bold, italic=italic, color=color, size=size))
            parts.append(run_xml(bmatch.group(1), bold=True, italic=italic, color=color, size=size))
            pos = bmatch.end()
        if pos < len(segment):
            parts.append(run_xml(segment[pos:], bold=bold, italic=italic, color=color, size=size))

    pos = 0
    for lmatch in _MD_LINK_RE.finditer(text):
        if lmatch.start() > pos:
            emit_plain(text[pos : lmatch.start()])
        label, url = lmatch.group(1), lmatch.group(2)
        safe_url = safe_hyperlink(url)
        if safe_url is not None:
            rid = f"rId{len(rel_registry) + 3}"
            rel_registry.append((rid, safe_url))
            run = run_xml(label, bold=bold, italic=italic, color="0563C1", size=size)
            parts.append(f'<w:hyperlink r:id="{rid}">{run}</w:hyperlink>')
        else:
            emit_plain(label)
        pos = lmatch.end()
    if pos < len(text):
        emit_plain(text[pos:])
    return "".join(parts)


def content_types_xml() -> str:
    """Return minimal ``[Content_Types].xml`` for a single-column Word document."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '<Override PartName="/word/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
        '<Override PartName="/word/numbering.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        "</Types>"
    )


def root_rels_xml() -> str:
    """Return package-level ``_rels/.rels`` linking ``document.xml`` and core metadata."""
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        "</Relationships>"
    )


def document_rels_xml(links: list[tuple[str, str]]) -> str:
    """Return ``word/_rels/document.xml.rels`` with style/numbering and external hyperlink targets."""
    extra = "".join(
        f'<Relationship Id="{rid}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        f'Target="{xml_escape(url)}" TargetMode="External"/>'
        for rid, url in links
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
        'Target="styles.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" '
        'Target="numbering.xml"/>'
        f"{extra}</Relationships>"
    )


def core_props_xml(title: str) -> str:
    """Return Dublin Core ``docProps/core.xml`` metadata for the generated ``.docx``."""
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{xml_escape(title)}</dc:title>"
        "<cp:lastModifiedBy>ULTRON ATS Builder</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def numbering_xml(font: str) -> str:
    """Return ``word/numbering.xml`` defining a standard single-level bullet list."""
    efont = xml_escape(font)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:abstractNum w:abstractNumId="0">'
        '<w:multiLevelType w:val="singleLevel"/>'
        '<w:lvl w:ilvl="0">'
        '<w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="\u2022"/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="360" w:hanging="180"/></w:pPr>'
        f'<w:rPr><w:rFonts w:ascii="{efont}" w:hAnsi="{efont}" w:hint="default"/></w:rPr>'
        "</w:lvl>"
        "</w:abstractNum>"
        '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
        "</w:numbering>"
    )
