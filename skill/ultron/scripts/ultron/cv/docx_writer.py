"""Pure-stdlib ATS-clean Word ``.docx`` CV builder.

Generates a single-column OOXML ``.docx`` with contact details in the document body,
zero tables, zero text boxes, zero images, zero headers/footers, and standard
Unicode bullets using an allowlisted ATS-safe font.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from ultron.cv import ats as cv_ats
from ultron.cv import docx_pages, docx_reader, layout_profile, preview
from ultron.cv import extract as cv
from ultron.cv import truth as cv_truth
from ultron.cv.layout_profile import MARGINS, PAGE_SIZES, CvLayoutStyle, merge_layout_overrides
from ultron.infra import fs, home, ooxml

WRITER_SAFE_FONTS = ooxml.WRITER_SAFE_FONTS
resolve_safe_font = ooxml.resolve_safe_font

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
    """One typed content block (paragraph, subheading, metadata, or bullets) inside a CV section."""
    kind: BlockKind
    text: str = ""
    items: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CvSection:
    """Titled section containing ordered CV content blocks."""
    heading: str
    blocks: list[CvBlock] = field(default_factory=list)


@dataclass(slots=True)
class CvDocument:
    """Normalized CV document ready for truthfulness checking and DOCX rendering."""
    name: str
    headline: str = ""
    contact: list[str] = field(default_factory=list)
    sections: list[CvSection] = field(default_factory=list)


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


_DENSITY_SPACING = {
    "compact": (40, 247, 120, 24, 247),
    "normal": (60, 252, 160, 40, 240),
    "spacious": (90, 269, 210, 65, 269),
}


def _section_decoration(layout: CvLayoutStyle) -> str:
    if layout.section_style == "rule":
        return f'<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="3" w:color="{layout.accent_color}"/></w:pBdr>'
    if layout.section_style == "shaded":
        return '<w:shd w:val="clear" w:fill="F3F5F7"/>'
    return ""


def _styles_xml(layout: CvLayoutStyle) -> str:
    efont = ooxml.xml_escape(layout.font)
    after, line, heading_before, bullet_after, bullet_line = _DENSITY_SPACING[layout.density]
    body_size, heading_size, name_size = (round(size * 2) for size in (
        layout.body_size_pt, layout.heading_size_pt, layout.name_size_pt,
    ))
    caps = "<w:caps/>" if layout.section_style == "caps" else ""
    rfonts = f'<w:rFonts w:ascii="{efont}" w:hAnsi="{efont}" w:cs="{efont}" w:eastAsia="{efont}"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        f"{rfonts}"
        f'<w:sz w:val="{body_size}"/><w:szCs w:val="{body_size}"/><w:color w:val="1F2937"/>'
        "</w:rPr></w:rPrDefault>"
        f'<w:pPrDefault><w:pPr><w:spacing w:after="{after}" w:line="{line}" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
        "</w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="0" w:after="40"/></w:pPr>'
        f'<w:rPr>{rfonts}<w:b/><w:color w:val="111827"/><w:sz w:val="{name_size}"/><w:szCs w:val="{name_size}"/></w:rPr>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="Heading2">'
        '<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
        f'<w:pPr><w:keepNext/><w:spacing w:before="{heading_before}" w:after="{after}"/>'
        f'{_section_decoration(layout)}</w:pPr>'
        f'<w:rPr>{rfonts}<w:b/>{caps}<w:color w:val="{layout.accent_color}"/>'
        f'<w:sz w:val="{heading_size}"/><w:szCs w:val="{heading_size}"/></w:rPr>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="ListParagraph">'
        '<w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:ind w:left="360" w:hanging="180"/>'
        f'<w:spacing w:after="{bullet_after}" w:line="{bullet_line}" w:lineRule="auto"/></w:pPr>'
        "</w:style>"
        "</w:styles>"
    )


def _render_role(
    title: str, date: str | None, links: list[tuple[str, str]], content_width: int,
) -> str:
    tabs = f'<w:tabs><w:tab w:val="right" w:pos="{content_width}"/></w:tabs>' if date else ""
    role = ooxml.inline_runs_xml(title, links, bold=True, color="111827", size=22)
    detail = ""
    if date:
        detail = "<w:r><w:tab/></w:r>" + ooxml.inline_runs_xml(date, links, italic=True, color="4B5563", size=20)
    return f'<w:p><w:pPr><w:keepNext/>{tabs}<w:spacing w:before="100" w:after="20"/></w:pPr>{role}{detail}</w:p>'


def _render_document_xml(doc: CvDocument, layout: CvLayoutStyle) -> tuple[str, list[tuple[str, str]]]:
    links: list[tuple[str, str]] = []
    width, height = PAGE_SIZES[layout.page_size]
    margin = MARGINS[layout.margins]
    alignment = '<w:jc w:val="center"/>' if layout.header_align == "center" else ""
    paragraphs: list[str] = [
        f'<w:p><w:pPr><w:pStyle w:val="Heading1"/>{alignment}</w:pPr>{ooxml.run_xml(doc.name)}</w:p>'
    ]
    if doc.headline:
        paragraphs.append(
            f'<w:p><w:pPr><w:spacing w:after="40"/>{alignment}</w:pPr>'
            f"{ooxml.inline_runs_xml(doc.headline, links, bold=True, color='374151', size=22)}</w:p>"
        )
    for line in doc.contact:
        paragraphs.append(
            f'<w:p><w:pPr><w:spacing w:after="60"/>{alignment}</w:pPr>'
            f"{ooxml.inline_runs_xml(line, links, color='4B5563', size=20)}</w:p>"
        )

    for section in doc.sections:
        paragraphs.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>{ooxml.run_xml(section.heading)}</w:p>'
        )
        blocks = iter(section.blocks)
        block = next(blocks, None)
        while block is not None:
            following = next(blocks, None)
            if block.kind == "subheading":
                date = None
                if layout.date_layout == "right_tab" and following is not None and following.kind == "meta":
                    date = following.text
                    following = next(blocks, None)
                paragraphs.append(_render_role(block.text, date, links, width - 2 * margin))
            elif block.kind == "meta":
                paragraphs.append(
                    '<w:p><w:pPr><w:keepNext/><w:spacing w:after="40"/></w:pPr>'
                    f"{ooxml.inline_runs_xml(block.text, links, italic=True, color='4B5563', size=20)}</w:p>"
                )
            elif block.kind == "paragraph":
                paragraphs.append(f"<w:p>{ooxml.inline_runs_xml(block.text, links)}</w:p>")
            elif block.kind == "bullets":
                for item in block.items:
                    paragraphs.append(
                        '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/>'
                        '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
                        f"{ooxml.inline_runs_xml(item, links)}</w:p>"
                    )
            block = following

    body = (
        "".join(paragraphs)
        + f'<w:sectPr><w:pgSz w:w="{width}" w:h="{height}"/>'
        + f'<w:pgMar w:top="{margin}" w:right="{margin}" w:bottom="{margin}" w:left="{margin}" '
        + 'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{body}</w:body></w:document>"
    )
    return xml, links


def render_docx_bytes(doc: CvDocument, *, font: str | None = None, layout: CvLayoutStyle | None = None) -> bytes:
    """Serialize ``doc`` into a clean single-column OOXML ``.docx`` archive."""
    selected = layout if layout is not None else CvLayoutStyle()
    if font is not None:
        selected = merge_layout_overrides(selected, font=font)
    doc_xml, links = _render_document_xml(doc, selected)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", ooxml.content_types_xml().encode("utf-8"))
        archive.writestr("_rels/.rels", ooxml.root_rels_xml().encode("utf-8"))
        archive.writestr("docProps/core.xml", ooxml.core_props_xml(doc.name).encode("utf-8"))
        archive.writestr("word/_rels/document.xml.rels", ooxml.document_rels_xml(links).encode("utf-8"))
        archive.writestr("word/styles.xml", _styles_xml(selected).encode("utf-8"))
        archive.writestr("word/numbering.xml", ooxml.numbering_xml(selected.font).encode("utf-8"))
        archive.writestr("word/document.xml", doc_xml.encode("utf-8"))
    return buf.getvalue()


def _build_layout(
    source: Path | None, layout: CvLayoutStyle | None, match_source: bool,
    overrides: dict[str, Any] | None, font: str | None,
) -> CvLayoutStyle:
    selected = layout if layout is not None else CvLayoutStyle()
    if match_source:
        if source is None:
            raise cv.CvExtractionError("--match-source-layout requires --source-cv", code="missing_source_cv")
        detected = layout_profile.extract_layout_profile(source, file_type=source.suffix)
        selected = CvLayoutStyle(**detected)
        if layout is not None:
            selected = merge_layout_overrides(selected, **asdict(layout))
    values = dict(overrides or {})
    if font is not None:
        values["font"] = font
    return merge_layout_overrides(selected, **values)


def build_ats_docx(
    from_path: Path,
    out_path: Path | None = None,
    *,
    font: str | None = None,
    layout: CvLayoutStyle | None = None,
    match_source_layout: bool = False,
    layout_overrides: dict[str, Any] | None = None,
    preview_output: bool = False,
    preview_dir: Path | None = None,
    dpi: int = 144,
    force: bool = False,
    source_cv: Path | None = None,
    confirmed_facts: list[str] | None = None,
    strict: bool = False,
) -> dict[str, Any]:
    """Build an ATS-clean ``.docx`` from a Markdown or JSON CV draft and verify round-trip parsing."""
    if strict and source_cv is None:
        raise cv.CvExtractionError("--strict requires --source-cv", code="missing_source_cv")
    src = Path(from_path)
    if not src.is_file():
        raise FileNotFoundError(f"draft CV file not found: {src}")
    if src.stat().st_size > cv.MAX_CV_BYTES:
        raise cv.CvExtractionError("CV draft file is too large", code="too_long")

    selected = _build_layout(source_cv, layout, match_source_layout, layout_overrides, font)
    if preview_output:
        preview.validate_preview_options(dpi)
    raw = cv.decode_text_bytes(src.read_bytes())
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

    docx_bytes = render_docx_bytes(doc, layout=selected)
    report = docx_reader.inspect_docx(docx_bytes) if preview_output else None
    count = docx_pages.measure_docx_pages(docx_bytes, report=report, keep_pdf=True) if preview_output else None
    audit = cv_ats.audit_generated_docx(docx_bytes, report=report, page_count=count)
    fs.atomic_write_bytes(target, docx_bytes)
    formatting_issues = [f for f in audit["findings"] if f["category"] == "format" and f["severity"] != "info"]
    return {
        "path": str(target),
        "font": selected.font,
        "layout_used": asdict(selected),
        **({"preview": preview.write_reference_preview(
            target, count.pdf_bytes, pages=count.pages, out_dir=preview_dir, dpi=dpi, warning=count.warning,
            rendered_pages_verified=count.source == "pdf_render",
        )} if count is not None else {}),
        "bytes": len(docx_bytes),
        "chars": audit["chars"],
        "pages": audit["pages"],
        "page_count_source": audit["page_count_source"],
        "page_count_converter": audit["page_count_converter"],
        **({"page_count_warning": audit["page_count_warning"]} if "page_count_warning" in audit else {}),
        "ats_audit": {
            "score": audit["score"],
            "format_tier": audit["format_tier"],
            "formatting_issues_count": len(formatting_issues),
        },
        "truth": truth_report,
    }
