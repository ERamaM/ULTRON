"""Pure-stdlib ATS-clean Word ``.docx`` CV builder.

Generates a single-column OOXML ``.docx`` with contact details in the document body,
zero tables, zero text boxes, zero images, zero headers/footers, and standard
Unicode bullets using an allowlisted ATS-safe font.
"""

from __future__ import annotations

import io
import json
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

from ultron import cv, cv_ats, cv_truth, home

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
_BULLET_RE = re.compile(r"^[ \t]*[-*•▪▫◦‣][ \t]+(\S.*)$")
_CONTACT_LINE_RE = re.compile(
    r"(@|https?://|linkedin\.com/|github\.com/|\||"
    r"\+\d{1,3}(?:[\s.-]?\(?\d{2,4}\)?){2,4}|"
    r"\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]\d{3,4}|"
    r"\d{3,4}[\s-]\d{3,4}[\s-]\d{3,4})",
    re.IGNORECASE,
)

BlockKind = Literal["subheading", "meta", "paragraph", "bullets"]


class TruthfulnessError(ValueError):
    """Raised when ``--strict`` refuses to build a ``.docx`` due to unverified draft claims."""

    def __init__(self, truth_report: dict[str, Any]) -> None:
        self.truth_report = truth_report
        count = len(truth_report.get("violations") or [])
        super().__init__(f"truthfulness verification failed with {count} violation(s)")


@dataclass(slots=True)
class CvBlock:
    kind: BlockKind
    text: str = ""
    items: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CvSection:
    heading: str
    blocks: list[CvBlock] = field(default_factory=list)


@dataclass(slots=True)
class CvDocument:
    name: str
    headline: str = ""
    contact: list[str] = field(default_factory=list)
    sections: list[CvSection] = field(default_factory=list)


def resolve_safe_font(font: str) -> str:
    """Validate ``font`` against ``WRITER_SAFE_FONTS`` and return canonical casing."""
    key = " ".join(str(font or "").split()).casefold()
    if key not in _SAFE_FONT_LOOKUP:
        allowed = ", ".join(WRITER_SAFE_FONTS)
        raise ValueError(f"unsupported font {font!r}; choose an ATS-safe font ({allowed})")
    return _SAFE_FONT_LOOKUP[key]


def _sanitize_xml_text(value: str) -> str:
    cleaned = str(value or "").replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    cleaned = _CONTROL_CHARS_RE.sub("", cleaned)
    return _SURROGATE_RE.sub("", cleaned)


def _xml_escape(value: str) -> str:
    return escape(_sanitize_xml_text(value), {'"': "&quot;", "'": "&apos;"})


def _safe_hyperlink(url: str) -> str | None:
    candidate = _sanitize_xml_text(url).strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"} and parsed.netloc:
        return candidate
    if scheme == "mailto" and "@" in parsed.path:
        return candidate
    return None


def parse_markdown_cv(text: str) -> CvDocument:
    """Parse structured Markdown CV text into a :class:`CvDocument`."""
    name = ""
    headline = ""
    contact: list[str] = []
    sections: list[CvSection] = []
    current_section: CvSection | None = None
    open_bullets: list[str] = []
    open_para: list[str] = []

    def flush_open() -> None:
        nonlocal open_bullets, open_para
        if current_section is None:
            open_bullets = []
            open_para = []
            return
        if open_bullets:
            current_section.blocks.append(CvBlock(kind="bullets", items=open_bullets))
            open_bullets = []
        if open_para:
            current_section.blocks.append(CvBlock(kind="paragraph", text=" ".join(open_para)))
            open_para = []

    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line:
            flush_open()
            continue
        if line.startswith("# ") and not name and current_section is None:
            name = line[2:].strip()
            continue
        if line.startswith("## "):
            flush_open()
            current_section = CvSection(heading=line[3:].strip())
            sections.append(current_section)
            continue
        if current_section is None:
            if not name:
                name = line.lstrip("#").strip()
            elif _CONTACT_LINE_RE.search(line):
                contact.append(line)
            elif not headline:
                headline = line
            else:
                contact.append(line)
            continue
        if line.startswith("### "):
            flush_open()
            current_section.blocks.append(CvBlock(kind="subheading", text=line[4:].strip()))
            continue
        if line.startswith("#### "):
            flush_open()
            current_section.blocks.append(CvBlock(kind="meta", text=line[5:].strip()))
            continue
        if bullet_match := _BULLET_RE.match(raw_line):
            if open_para:
                flush_open()
            open_bullets.append(bullet_match.group(1).strip())
            continue
        if open_bullets:
            flush_open()
        open_para.append(line)

    flush_open()
    if not name and not sections:
        raise ValueError("CV input has no name or sections to render")
    return CvDocument(name=name or "Candidate CV", headline=headline, contact=contact, sections=sections)


def parse_json_cv(data: dict[str, Any]) -> CvDocument:
    """Parse a structured JSON CV object into a :class:`CvDocument`."""
    if not isinstance(data, dict):
        raise TypeError("JSON CV must be an object")
    name = str(data.get("name") or "").strip()
    headline = str(data.get("headline") or "").strip()
    raw_contact = data.get("contact")
    contact: list[str] = []
    if isinstance(raw_contact, str) and raw_contact.strip():
        contact.append(raw_contact.strip())
    elif isinstance(raw_contact, list):
        joined = " | ".join(str(item).strip() for item in raw_contact if str(item).strip())
        if joined:
            contact.append(joined)
    elif isinstance(raw_contact, dict):
        joined = " | ".join(str(v).strip() for v in raw_contact.values() if str(v).strip())
        if joined:
            contact.append(joined)

    sections: list[CvSection] = []
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        heading = str(sec.get("heading") or "").strip()
        if not heading:
            continue
        blocks: list[CvBlock] = []
        for raw_block in sec.get("blocks") or []:
            if isinstance(raw_block, str) and raw_block.strip():
                blocks.append(CvBlock(kind="paragraph", text=raw_block.strip()))
            elif isinstance(raw_block, dict):
                kind = str(raw_block.get("kind") or "paragraph")
                if kind == "bullets":
                    items = [str(i).strip() for i in (raw_block.get("items") or []) if str(i).strip()]
                    if items:
                        blocks.append(CvBlock(kind="bullets", items=items))
                elif kind in {"subheading", "meta", "paragraph"}:
                    txt = str(raw_block.get("text") or "").strip()
                    if txt:
                        blocks.append(CvBlock(kind=kind, text=txt))  # type: ignore[arg-type]
        sections.append(CvSection(heading=heading, blocks=blocks))

    if not name and not sections:
        raise ValueError("JSON CV has no name or sections to render")
    return CvDocument(name=name or "Candidate CV", headline=headline, contact=contact, sections=sections)


def _run_xml(text: str, *, bold: bool = False, italic: bool = False, color: str | None = None, size: int | None = None) -> str:
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
    return f'<w:r>{rpr}<w:t xml:space="preserve">{_xml_escape(text)}</w:t></w:r>'


def _inline_runs_xml(
    text: str,
    rel_registry: list[tuple[str, str]],
    *,
    bold: bool = False,
    italic: bool = False,
    color: str | None = None,
    size: int | None = None,
) -> str:
    parts: list[str] = []

    def emit_plain(segment: str) -> None:
        pos = 0
        for bmatch in _BOLD_RE.finditer(segment):
            if bmatch.start() > pos:
                parts.append(_run_xml(segment[pos : bmatch.start()], bold=bold, italic=italic, color=color, size=size))
            parts.append(_run_xml(bmatch.group(1), bold=True, italic=italic, color=color, size=size))
            pos = bmatch.end()
        if pos < len(segment):
            parts.append(_run_xml(segment[pos:], bold=bold, italic=italic, color=color, size=size))

    pos = 0
    for lmatch in _MD_LINK_RE.finditer(text):
        if lmatch.start() > pos:
            emit_plain(text[pos : lmatch.start()])
        label, url = lmatch.group(1), lmatch.group(2)
        safe_url = _safe_hyperlink(url)
        if safe_url is not None:
            rid = f"rId{len(rel_registry) + 3}"
            rel_registry.append((rid, safe_url))
            run = _run_xml(label, bold=bold, italic=italic, color="0563C1", size=size)
            parts.append(f'<w:hyperlink r:id="{rid}">{run}</w:hyperlink>')
        else:
            emit_plain(label)
        pos = lmatch.end()
    if pos < len(text):
        emit_plain(text[pos:])
    return "".join(parts)


def _content_types_xml() -> str:
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


def _root_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>'
        '<Relationship Id="rId2" '
        'Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
        'Target="docProps/core.xml"/>'
        "</Relationships>"
    )


def _document_rels_xml(links: list[tuple[str, str]]) -> str:
    extra = "".join(
        f'<Relationship Id="{rid}" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
        f'Target="{_xml_escape(url)}" TargetMode="External"/>'
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


def _core_props_xml(title: str) -> str:
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{_xml_escape(title)}</dc:title>"
        "<cp:lastModifiedBy>ULTRON ATS Builder</cp:lastModifiedBy>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )


def _styles_xml(font: str) -> str:
    efont = _xml_escape(font)
    rfonts = f'<w:rFonts w:ascii="{efont}" w:hAnsi="{efont}" w:cs="{efont}" w:eastAsia="{efont}"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        f"{rfonts}"
        '<w:sz w:val="21"/><w:szCs w:val="21"/><w:color w:val="1F2937"/>'
        "</w:rPr></w:rPrDefault>"
        '<w:pPrDefault><w:pPr><w:spacing w:after="60" w:line="252" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
        "</w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="0" w:after="40"/></w:pPr>'
        f'<w:rPr>{rfonts}<w:b/><w:color w:val="111827"/><w:sz w:val="34"/><w:szCs w:val="34"/></w:rPr>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="Heading2">'
        '<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="160" w:after="60"/>'
        '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="3" w:color="1F3A5F"/></w:pBdr></w:pPr>'
        f'<w:rPr>{rfonts}<w:b/><w:color w:val="1F3A5F"/><w:sz w:val="23"/><w:szCs w:val="23"/></w:rPr>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="ListParagraph">'
        '<w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:ind w:left="360" w:hanging="180"/><w:spacing w:after="40" w:line="240" w:lineRule="auto"/></w:pPr>'
        "</w:style>"
        "</w:styles>"
    )


def _numbering_xml(font: str) -> str:
    efont = _xml_escape(font)
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


def _render_document_xml(doc: CvDocument) -> tuple[str, list[tuple[str, str]]]:
    links: list[tuple[str, str]] = []
    paragraphs: list[str] = [
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>{_run_xml(doc.name)}</w:p>'
    ]
    if doc.headline:
        paragraphs.append(
            f'<w:p><w:pPr><w:spacing w:after="40"/></w:pPr>'
            f"{_inline_runs_xml(doc.headline, links, bold=True, color='374151', size=22)}</w:p>"
        )
    for line in doc.contact:
        paragraphs.append(
            f'<w:p><w:pPr><w:spacing w:after="60"/></w:pPr>'
            f"{_inline_runs_xml(line, links, color='4B5563', size=20)}</w:p>"
        )

    for section in doc.sections:
        paragraphs.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>{_run_xml(section.heading)}</w:p>'
        )
        for block in section.blocks:
            if block.kind == "subheading":
                paragraphs.append(
                    '<w:p><w:pPr><w:keepNext/><w:spacing w:before="100" w:after="20"/></w:pPr>'
                    f"{_inline_runs_xml(block.text, links, bold=True, color='111827', size=22)}</w:p>"
                )
            elif block.kind == "meta":
                paragraphs.append(
                    '<w:p><w:pPr><w:keepNext/><w:spacing w:after="40"/></w:pPr>'
                    f"{_inline_runs_xml(block.text, links, italic=True, color='4B5563', size=20)}</w:p>"
                )
            elif block.kind == "paragraph":
                paragraphs.append(f"<w:p>{_inline_runs_xml(block.text, links)}</w:p>")
            elif block.kind == "bullets":
                for item in block.items:
                    paragraphs.append(
                        '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/>'
                        '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
                        f"{_inline_runs_xml(item, links)}</w:p>"
                    )

    body = (
        "".join(paragraphs)
        + '<w:sectPr><w:pgSz w:w="12240" w:h="15840"/>'
        + '<w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" '
        + 'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{body}</w:body></w:document>"
    )
    return xml, links


def render_docx_bytes(doc: CvDocument, *, font: str = "Calibri") -> bytes:
    """Serialize ``doc`` into a clean single-column OOXML ``.docx`` archive."""
    safe_font = resolve_safe_font(font)
    doc_xml, links = _render_document_xml(doc)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types_xml().encode("utf-8"))
        archive.writestr("_rels/.rels", _root_rels_xml().encode("utf-8"))
        archive.writestr("docProps/core.xml", _core_props_xml(doc.name).encode("utf-8"))
        archive.writestr("word/_rels/document.xml.rels", _document_rels_xml(links).encode("utf-8"))
        archive.writestr("word/styles.xml", _styles_xml(safe_font).encode("utf-8"))
        archive.writestr("word/numbering.xml", _numbering_xml(safe_font).encode("utf-8"))
        archive.writestr("word/document.xml", doc_xml.encode("utf-8"))
    return buf.getvalue()


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def build_ats_docx(
    from_path: Path,
    out_path: Path | None = None,
    *,
    font: str = "Calibri",
    force: bool = False,
    source_cv: Path | None = None,
    confirmed_facts: list[str] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Build an ATS-clean ``.docx`` from a Markdown or JSON CV draft and verify round-trip parsing."""
    src = Path(from_path)
    if not src.is_file():
        raise FileNotFoundError(f"draft CV file not found: {src}")
    if src.stat().st_size > cv.MAX_CV_BYTES:
        raise cv.CvExtractionError("CV draft file is too large", code="too_long")

    safe_font = resolve_safe_font(font)
    raw = cv._decode_text_bytes(src.read_bytes())
    if src.suffix.lower() == ".json":
        doc = parse_json_cv(json.loads(raw))
    else:
        doc = parse_markdown_cv(raw)

    stem = re.sub(r"-ats$", "", src.stem) or "cv"
    target = Path(out_path) if out_path is not None else (home.ultron_home() / "cv" / "optimized" / f"{stem}-ats.docx")
    if target.exists() and not force:
        raise FileExistsError(f"output file already exists: {target} (pass --force to overwrite)")

    truth_report: dict[str, Any] | None = None
    if source_cv is not None:
        truth_report = cv_truth.verify_truth(Path(source_cv), src, confirmed_facts=confirmed_facts)
        if strict and not truth_report["passed"]:
            raise TruthfulnessError(truth_report)

    docx_bytes = render_docx_bytes(doc, font=safe_font)
    _atomic_write_bytes(target, docx_bytes)

    audit = cv_ats.audit_cv(target, write_report=False)
    formatting_issues = [f for f in audit["findings"] if f["category"] == "format" and f["severity"] != "info"]
    return {
        "path": str(target),
        "font": safe_font,
        "bytes": len(docx_bytes),
        "chars": audit["chars"],
        "pages": audit["pages"],
        "ats_audit": {
            "score": audit["score"],
            "format_tier": audit["format_tier"],
            "formatting_issues_count": len(formatting_issues),
        },
        "truth": truth_report,
    }
