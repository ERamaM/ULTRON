"""Bundled single-column Word (.docx) templates and hardened custom template renderer."""

from __future__ import annotations

import io
import posixpath
import re
import zipfile
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any, Protocol
from xml.etree.ElementTree import ParseError

from defusedxml import ElementTree, minidom
from defusedxml.common import DefusedXmlException

from ultron.cv import docx_reader as cv_docx
from ultron.cv import extract as cv
from ultron.cv.layout_profile import PAGE_SIZES
from ultron.infra import fs, ooxml


class CoverLetterFields(Protocol):
    """Field contract required by cover-letter DOCX and Markdown template renderers."""
    candidate_name: str
    candidate_email: str
    candidate_phone: str
    candidate_location: str
    candidate_links: list[str]
    date: str
    recipient_name: str
    recipient_title: str
    company: str
    role: str
    subject: str
    salutation: str
    opening: str
    paragraphs: list[str]
    highlights: list[str]
    closing: str
    signoff: str

TEMPLATE_PLACEHOLDERS: tuple[str, ...] = (
    "candidate_name",
    "candidate_email",
    "candidate_phone",
    "candidate_location",
    "candidate_links",
    "date",
    "recipient_name",
    "recipient_title",
    "company",
    "role",
    "subject",
    "salutation",
    "opening",
    "paragraphs",
    "highlights",
    "closing",
    "signoff",
    "body",
)
_PLACEHOLDER_SET = frozenset(TEMPLATE_PLACEHOLDERS)
_MULTI_PLACEHOLDERS = frozenset({"body", "paragraphs", "highlights"})
_OPTIONAL_STANDALONE = frozenset({
    "candidate_email",
    "candidate_phone",
    "candidate_location",
    "candidate_links",
    "date",
    "recipient_name",
    "recipient_title",
    "company",
    "role",
    "subject",
})

BUNDLED_TEMPLATES: dict[str, dict[str, Any]] = {
    "classic": {
        "name": "classic",
        "default_font": "Georgia",
        "accent": "374151",
        "description": "Traditional serif letterhead with crisp divider rule and formal executive spacing.",
        "ats_notes": "Single-column body-only OOXML (0 tables, 0 textboxes, ATS-safe Georgia).",
    },
    "modern": {
        "name": "modern",
        "default_font": "Calibri",
        "accent": "1B365D",
        "description": "Clean contemporary hierarchy in Calibri with deep-navy accent border on subject callout.",
        "ats_notes": "Single-column body-only OOXML (0 tables, 0 textboxes, ATS-safe Calibri).",
    },
    "executive": {
        "name": "executive",
        "default_font": "Cambria",
        "accent": "1E293B",
        "description": "Dark-slate full-width shaded top header banner paired with balanced executive typography.",
        "ats_notes": "Single-column body-only OOXML using paragraph shading (0 tables, 0 textboxes, Cambria).",
    },
}

MAX_TEMPLATE_ARCHIVE_BYTES = 5 * 1024 * 1024
MAX_TEMPLATE_TOTAL_UNCOMPRESSED = 16 * 1024 * 1024
_CHUNK = 64 * 1024
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD_TEXT_PART_RE = re.compile(r"^word/(?:document|header\d{1,3}|footer\d{1,3})\.xml$")
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_WORD_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_HYPERLINK_REL = f"{_WORD_REL_NS}/hyperlink"
_FIXED_PARTS = {
    "word/document.xml": f"{_WORD_REL_NS}/officeDocument",
    "docProps/core.xml": f"{_PACKAGE_REL_NS}/metadata/core-properties",
    "docProps/app.xml": f"{_WORD_REL_NS}/extended-properties",

    "word/stylesWithEffects.xml": "http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects",
    **{f"word/{name}.xml": f"{_WORD_REL_NS}/{name}"
       for name in ("styles", "numbering", "settings", "webSettings", "fontTable")},
}
_DECORATION_PART_RE = re.compile(
    r"word/(?:theme/theme\d{1,3}\.xml|media/[A-Za-z0-9_.-]{1,120}\.(?:png|jpg|jpeg|gif|bmp))"
)
_LAYOUT_RELS_RE = re.compile(r"word/_rels/(?:document|header\d{1,3}|footer\d{1,3}|numbering)\.xml\.rels")
_REVIEW_MARKUP = (
    "commentRangeStart", "commentRangeEnd", "commentReference", "ins", "del", "moveFrom", "moveTo",
    "pPrChange", "rPrChange", "sectPrChange", "tblPrChange", "trPrChange", "tcPrChange",
)
_PLACEHOLDER_RE = re.compile(r"\{\{[ \t]{0,4}([a-z_]{1,32})[ \t]{0,4}\}\}")


def list_templates() -> dict[str, Any]:
    """Return metadata for the bundled `.docx` templates and supported custom placeholders."""
    return {
        "templates": [dict(spec) for spec in BUNDLED_TEMPLATES.values()],
        "placeholders": [f"{{{{{name}}}}}" for name in TEMPLATE_PLACEHOLDERS],
        "multiline_placeholders": ["{{body}}", "{{paragraphs}}", "{{highlights}}"],
        "safe_fonts": sorted(ooxml.WRITER_SAFE_FONTS),
    }


def _cover_letter_styles_xml(font: str, accent: str) -> str:
    efont = ooxml.xml_escape(font)
    rfonts = f'<w:rFonts w:ascii="{efont}" w:hAnsi="{efont}" w:cs="{efont}" w:eastAsia="{efont}"/>'
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:docDefaults><w:rPrDefault><w:rPr>'
        f"{rfonts}"
        '<w:sz w:val="21"/><w:szCs w:val="21"/><w:color w:val="1F2937"/>'
        "</w:rPr></w:rPrDefault>"
        '<w:pPrDefault><w:pPr><w:spacing w:after="140" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
        "</w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
        '<w:name w:val="Normal"/>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="Heading1">'
        '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:keepNext/><w:spacing w:before="0" w:after="40"/></w:pPr>'
        f'<w:rPr>{rfonts}<w:b/><w:color w:val="{accent}"/><w:sz w:val="32"/><w:szCs w:val="32"/></w:rPr>'
        "</w:style>"
        '<w:style w:type="paragraph" w:styleId="ListParagraph">'
        '<w:name w:val="List Paragraph"/><w:basedOn w:val="Normal"/>'
        '<w:pPr><w:ind w:left="360" w:hanging="180"/><w:spacing w:after="80" w:line="252" w:lineRule="auto"/></w:pPr>'
        "</w:style>"
        "</w:styles>"
    )


def _header_paragraphs(doc: CoverLetterFields, style: dict[str, Any], template_name: str) -> list[str]:
    contact_parts = [
        part
        for part in (
            doc.candidate_email,
            doc.candidate_phone,
            doc.candidate_location,
            " | ".join(doc.candidate_links),
        )
        if part and part.strip()
    ]
    contact_line = "  |  ".join(contact_parts)
    paragraphs: list[str] = []

    if template_name == "executive":
        shd = '<w:shd w:val="clear" w:color="auto" w:fill="1E293B"/>'
        if doc.candidate_name:
            paragraphs.append(
                f'<w:p><w:pPr>{shd}<w:spacing w:before="140" w:after="40"/><w:ind w:left="140" w:right="140"/></w:pPr>'
                f"{ooxml.run_xml(doc.candidate_name, bold=True, color='FFFFFF', size=34)}</w:p>"
            )
        if contact_line:
            paragraphs.append(
                f'<w:p><w:pPr>{shd}<w:spacing w:before="0" w:after="140"/><w:ind w:left="140" w:right="140"/></w:pPr>'
                f"{ooxml.run_xml(contact_line, color='E2E8F0', size=19)}</w:p>"
            )
        paragraphs.append(
            '<w:p><w:pPr><w:spacing w:before="0" w:after="160"/>'
            '<w:pBdr><w:bottom w:val="single" w:sz="8" w:space="4" w:color="CBD5E1"/></w:pBdr></w:pPr></w:p>'
        )
        return paragraphs

    accent = style["accent"]
    align = '<w:jc w:val="center"/>' if template_name == "classic" else ""
    if doc.candidate_name:
        paragraphs.append(
            f'<w:p><w:pPr>{align}<w:spacing w:before="0" w:after="40"/></w:pPr>'
            f"{ooxml.run_xml(doc.candidate_name, bold=True, color=accent, size=32)}</w:p>"
        )
    if contact_line:
        bdr = (
            f'<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="6" w:color="{accent}"/></w:pBdr>'
        )
        paragraphs.append(
            f'<w:p><w:pPr>{align}{bdr}<w:spacing w:before="0" w:after="180"/></w:pPr>'
            f"{ooxml.run_xml(contact_line, color='4B5563', size=19)}</w:p>"
        )
    return paragraphs


def _recipient_and_subject_paragraphs(
    doc: CoverLetterFields, style: dict[str, Any], template_name: str, links: list[tuple[str, str]]
) -> list[str]:
    paragraphs: list[str] = []
    if doc.date:
        paragraphs.append(
            '<w:p><w:pPr><w:spacing w:after="140"/></w:pPr>'
            f"{ooxml.run_xml(doc.date, color='4B5563', size=20)}</w:p>"
        )
    recipient_lines = [ln for ln in (doc.recipient_name, doc.recipient_title, doc.company) if ln and ln.strip()]
    for idx, line in enumerate(recipient_lines):
        after = "160" if idx == len(recipient_lines) - 1 else "20"
        bold = idx == 0 or (not doc.recipient_name and line == doc.company)
        paragraphs.append(
            f'<w:p><w:pPr><w:spacing w:after="{after}"/></w:pPr>'
            f"{ooxml.run_xml(line, bold=bold, color='111827', size=21)}</w:p>"
        )

    subject_line = doc.subject or (f"Application: {doc.role}" if doc.role else "")
    if subject_line:
        accent = style["accent"]
        if template_name == "modern":
            ppr = (
                '<w:pPr><w:spacing w:before="40" w:after="180"/>'
                '<w:ind w:left="140"/>'
                f'<w:pBdr><w:left w:val="single" w:sz="18" w:space="8" w:color="{accent}"/></w:pBdr></w:pPr>'
            )
        else:
            ppr = '<w:pPr><w:spacing w:before="40" w:after="180"/></w:pPr>'
        paragraphs.append(
            f"<w:p>{ppr}{ooxml.inline_runs_xml(subject_line, links, bold=True, color=accent, size=22)}</w:p>"
        )
    return paragraphs


def _body_paragraphs(doc: CoverLetterFields, links: list[tuple[str, str]]) -> list[str]:
    paragraphs: list[str] = []
    if doc.salutation:
        paragraphs.append(
            '<w:p><w:pPr><w:spacing w:after="140"/></w:pPr>'
            f"{ooxml.run_xml(doc.salutation, bold=True, color='111827')}</w:p>"
        )

    prose = [p for p in [doc.opening, *doc.paragraphs] if p and p.strip()]
    for para in prose:
        paragraphs.append(f"<w:p>{ooxml.inline_runs_xml(para, links)}</w:p>")

    for bullet in doc.highlights:
        if bullet.strip():
            paragraphs.append(
                '<w:p><w:pPr><w:pStyle w:val="ListParagraph"/>'
                '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>'
                f"{ooxml.inline_runs_xml(bullet.strip(), links)}</w:p>"
            )

    if doc.closing:
        paragraphs.append(f"<w:p>{ooxml.inline_runs_xml(doc.closing, links)}</w:p>")
    if doc.signoff:
        paragraphs.append(
            '<w:p><w:pPr><w:keepNext/><w:spacing w:before="100" w:after="60"/></w:pPr>'
            f"{ooxml.run_xml(doc.signoff)}</w:p>"
        )
    if doc.candidate_name:
        paragraphs.append(
            '<w:p><w:pPr><w:spacing w:after="0"/></w:pPr>'
            f"{ooxml.run_xml(doc.candidate_name, bold=True, color='111827')}</w:p>"
        )
    return paragraphs


def _package_cover_letter_docx(
    title: str,
    safe_font: str,
    styles_xml: str,
    document_xml: str,
    links: list[tuple[str, str]],
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", ooxml.content_types_xml().encode("utf-8"))
        archive.writestr("_rels/.rels", ooxml.root_rels_xml().encode("utf-8"))
        archive.writestr("docProps/core.xml", ooxml.core_props_xml(title).encode("utf-8"))
        archive.writestr("word/_rels/document.xml.rels", ooxml.document_rels_xml(links).encode("utf-8"))
        archive.writestr("word/styles.xml", styles_xml.encode("utf-8"))
        archive.writestr("word/numbering.xml", ooxml.numbering_xml(safe_font).encode("utf-8"))
        archive.writestr("word/document.xml", document_xml.encode("utf-8"))
    return buf.getvalue()


def render_bundled_docx_bytes(
    doc: CoverLetterFields, *, template_name: str = "modern", font: str | None = None, page_size: str = "letter",
) -> bytes:
    """Serialize ``doc`` into an ATS-safe single-column ``.docx`` using one of the bundled templates."""
    key = template_name.strip().lower()
    if key not in BUNDLED_TEMPLATES:
        raise ValueError(f"unknown template {template_name!r}; choose from {', '.join(BUNDLED_TEMPLATES)}")
    if page_size not in PAGE_SIZES:
        raise ValueError("page_size must be letter or a4.")
    width, height = PAGE_SIZES[page_size]
    style = BUNDLED_TEMPLATES[key]
    safe_font = ooxml.resolve_safe_font(font or style["default_font"])
    links: list[tuple[str, str]] = []
    paragraphs = [
        *_header_paragraphs(doc, style, key),
        *_recipient_and_subject_paragraphs(doc, style, key, links),
        *_body_paragraphs(doc, links),
    ]
    body = (
        "".join(paragraphs)
        + f'<w:sectPr><w:pgSz w:w="{width}" w:h="{height}"/>'
        + '<w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080" '
        + 'w:header="720" w:footer="720" w:gutter="0"/></w:sectPr>'
    )
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<w:body>{body}</w:body></w:document>"
    )
    styles_xml = _cover_letter_styles_xml(safe_font, style["accent"])
    doc_title = doc.subject or f"Cover Letter - {doc.company or doc.candidate_name or 'ULTRON'}"
    return _package_cover_letter_docx(doc_title, safe_font, styles_xml, doc_xml, links)


def export_starter_templates(
    out_dir: Path, *, template: str | None = None, force: bool = False
) -> dict[str, Any]:
    """Write starter `.docx` template files containing `{{placeholder}}` tags to ``out_dir``."""
    selected = [template.strip().lower()] if template else list(BUNDLED_TEMPLATES)
    for name in selected:
        if name not in BUNDLED_TEMPLATES:
            raise ValueError(f"unknown template {name!r}; choose from {', '.join(BUNDLED_TEMPLATES)}")

    target_dir = Path(out_dir)
    paths = [target_dir / f"cover-letter-{name}-template.docx" for name in selected]
    if not force:
        for path in paths:
            if path.exists():
                raise FileExistsError(f"template file already exists: {path} (pass --force to overwrite)")

    starter: Any = SimpleNamespace(
        candidate_name="{{candidate_name}}",
        candidate_email="{{candidate_email}}",
        candidate_phone="{{candidate_phone}}",
        candidate_location="{{candidate_location}}",
        candidate_links=["{{candidate_links}}"],
        date="{{date}}",
        recipient_name="{{recipient_name}}",
        recipient_title="{{recipient_title}}",
        company="{{company}}",
        role="{{role}}",
        subject="{{subject}}",
        salutation="{{salutation}}",
        opening="{{opening}}",
        paragraphs=["{{paragraphs}}"],
        highlights=["{{highlights}}"],
        closing="{{closing}}",
        signoff="{{signoff}}",
    )

    exported: list[dict[str, Any]] = []
    for name, dest in zip(selected, paths, strict=True):
        raw = render_bundled_docx_bytes(starter, template_name=name)
        fs.atomic_write_bytes(dest, raw)
        exported.append({"template": name, "path": str(dest.resolve()), "bytes": len(raw)})

    return {
        "ok": True,
        "exported": exported,
        "placeholders": [f"{{{{{item}}}}}" for item in TEMPLATE_PLACEHOLDERS],
    }


def _validate_archive_entry_name(name: str) -> None:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise ValueError(f"unsafe ZIP entry path in custom template: {name!r}")
    parts = PurePosixPath(name).parts
    if ".." in parts:
        raise ValueError(f"path traversal entry in custom template: {name!r}")


def _read_template_entries(template_path: Path) -> list[tuple[str, bytes]]:
    src = Path(template_path)
    if not src.is_file():
        raise FileNotFoundError(f"custom template file not found: {src}")
    if src.stat().st_size > MAX_TEMPLATE_ARCHIVE_BYTES:
        raise cv.CvExtractionError("custom template file exceeds 5 MiB limit", code="too_long")

    raw_bytes = src.read_bytes()
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_bytes))
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        raise ValueError("custom template is not a readable .docx ZIP archive") from exc

    with archive:
        entries = archive.infolist()
        if len(entries) > cv_docx.MAX_ARCHIVE_ENTRIES:
            raise ValueError("custom template archive has too many entries")
        seen: set[str] = set()
        total_uncompressed = 0
        loaded: list[tuple[str, bytes]] = []
        for info in entries:
            _validate_archive_entry_name(info.filename)
            if info.filename in seen:
                raise ValueError(f"duplicate ZIP entry in custom template: {info.filename!r}")
            seen.add(info.filename)
            if info.flag_bits & 0x1:
                raise ValueError("custom template contains encrypted ZIP entries")
            if info.compress_type not in cv_docx._ALLOWED_COMPRESSION:
                raise ValueError("custom template uses an unsupported ZIP compression method")
            if info.filename == "word/document.xml":
                entry_cap = cv_docx.MAX_DOCUMENT_XML_BYTES
            elif _WORD_TEXT_PART_RE.match(info.filename):
                entry_cap = cv_docx.MAX_AUX_XML_BYTES
            else:
                entry_cap = MAX_TEMPLATE_ARCHIVE_BYTES
            if info.file_size > entry_cap:
                raise ValueError(f"custom template entry {info.filename!r} exceeds inflated size cap")
            data = bytearray()
            with archive.open(info) as stream:
                while chunk := stream.read(_CHUNK):
                    data += chunk
                    total_uncompressed += len(chunk)
                    if len(data) > entry_cap or total_uncompressed > MAX_TEMPLATE_TOTAL_UNCOMPRESSED:
                        raise ValueError("custom template decompressed XML exceeds safety size limit")
            loaded.append((info.filename, bytes(data)))

    if "word/document.xml" not in seen:
        raise ValueError("custom template archive has no word/document.xml")
    return loaded


def _layout_relationship_type(part: str) -> str | None:
    if part in _FIXED_PARTS:
        return _FIXED_PARTS[part]
    if _WORD_TEXT_PART_RE.fullmatch(part):
        return f"{_WORD_REL_NS}/{'header' if part.startswith('word/header') else 'footer'}"
    if _DECORATION_PART_RE.fullmatch(part):
        return f"{_WORD_REL_NS}/{'image' if part.startswith('word/media/') else 'theme'}"
    return None


def _validate_template_relationships(name: str, data: bytes, parts: set[str]) -> None:
    try:
        root = ElementTree.fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException) as exc:
        raise ValueError("unsupported custom template relationship XML") from exc
    if root.tag != f"{{{_PACKAGE_REL_NS}}}Relationships":
        raise ValueError("unsupported custom template relationship XML")
    for element in root:
        rel_type = element.get("Type", "")
        target = element.get("Target", "")
        mode = element.get("TargetMode", "Internal")
        if element.tag != f"{{{_PACKAGE_REL_NS}}}Relationship":
            raise ValueError("unsupported custom template relationship XML")
        if mode == "External":
            if target and name != "_rels/.rels" and rel_type == _HYPERLINK_REL and ooxml.safe_hyperlink(target) == target:
                continue
        elif mode == "Internal" and target and not re.search(r"[\\:%?#\s]", target):
            base = "" if name == "_rels/.rels" or target.startswith("/") else "word"
            part = posixpath.normpath(posixpath.join(base, target.lstrip("/")))
            expected = _layout_relationship_type(part)
            legacy_core = part == "docProps/core.xml" and rel_type == f"{_WORD_REL_NS}/metadata/core-properties"
            if part in parts and (rel_type == expected or legacy_core):
                continue
        raise ValueError("unsupported custom template relationship")


def _prepare_template_part(name: str, data: bytes, parts: set[str], title: str) -> bytes:
    if name == "docProps/core.xml":
        return ooxml.core_props_xml(title).encode("utf-8")
    if name == "docProps/app.xml":
        return b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"/>'
    if name == "_rels/.rels" or _LAYOUT_RELS_RE.fullmatch(name):
        _validate_template_relationships(name, data, parts)
    elif name != "[Content_Types].xml" and _layout_relationship_type(name) is None:
        raise ValueError(f"unsupported custom template part: {name}")
    return data


def _direct_runs(p_node: minidom.Element) -> list[tuple[minidom.Element, minidom.Element, str]]:
    """Collect ``(r_node, t_node, text)`` for runs belonging directly to ``p_node`` (excluding nested ``w:p``)."""
    collected: list[tuple[minidom.Element, minidom.Element, str]] = []

    def walk(node: minidom.Node) -> None:
        for child in node.childNodes:
            if child.nodeType != child.ELEMENT_NODE:
                continue
            if child.namespaceURI == _W_NS and child.localName == "p":
                continue
            if child.namespaceURI == _W_NS and child.localName == "r":
                for sub in child.childNodes:
                    if sub.nodeType == sub.ELEMENT_NODE and sub.namespaceURI == _W_NS and sub.localName == "t":
                        text = "".join(
                            t.data for t in sub.childNodes if t.nodeType in (t.TEXT_NODE, t.CDATA_SECTION_NODE)
                        )
                        collected.append((child, sub, text))
            else:
                walk(child)

    walk(p_node)
    return collected


def _set_t_text(dom: minidom.Document, t_node: minidom.Element, text: str) -> None:
    for child in list(t_node.childNodes):
        t_node.removeChild(child)
    t_node.appendChild(dom.createTextNode(text))
    t_node.setAttribute("xml:space", "preserve")


def _has_sibling_paragraph(p_node: minidom.Element) -> bool:
    parent = p_node.parentNode
    if parent is None:
        return False
    return sum(
        1
        for child in parent.childNodes
        if child.nodeType == child.ELEMENT_NODE and child.namespaceURI == _W_NS and child.localName == "p"
    ) > 1


def _has_num_pr(p_node: minidom.Element) -> bool:
    for child in p_node.childNodes:
        if child.nodeType == child.ELEMENT_NODE and child.namespaceURI == _W_NS and child.localName == "pPr":
            for prop in child.childNodes:
                if prop.nodeType == prop.ELEMENT_NODE and prop.namespaceURI == _W_NS and prop.localName == "numPr":
                    return True
    return False


def _replace_spans_in_runs(
    dom: minidom.Document,
    runs: list[tuple[minidom.Element, minidom.Element, str]],
    matches: list[re.Match[str]],
    values: dict[str, str],
) -> None:
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for _r, _t, txt in runs:
        offsets.append((cursor, cursor + len(txt)))
        cursor += len(txt)
    texts = [txt for _r, _t, txt in runs]

    for match in reversed(matches):
        m_start, m_end = match.span()
        replacement = ooxml.sanitize_xml_text(values[match.group(1)])
        first_idx = next(idx for idx, (_s, end) in enumerate(offsets) if end > m_start)
        last_idx = next(idx for idx in range(len(offsets) - 1, -1, -1) if offsets[idx][0] < m_end)
        f_start, _f_end = offsets[first_idx]
        l_start, _l_end = offsets[last_idx]
        prefix = texts[first_idx][: m_start - f_start]
        suffix = texts[last_idx][m_end - l_start :]
        if first_idx == last_idx:
            texts[first_idx] = prefix + replacement + suffix
        else:
            texts[first_idx] = prefix + replacement
            for mid in range(first_idx + 1, last_idx):
                texts[mid] = ""
            texts[last_idx] = suffix

    for (r_node, t_node, orig), new_txt in zip(runs, texts, strict=True):
        if new_txt != orig:
            _set_t_text(dom, t_node, new_txt)
            if (
                not new_txt
                and r_node.parentNode is not None
                and len(runs) > 1
                and not any(
                    c.nodeType == c.ELEMENT_NODE and c.localName not in {"rPr", "t"}
                    for c in r_node.childNodes
                )
            ):
                # Only drop completely emptied run when parent is not holding graphics/breaks.
                r_node.parentNode.removeChild(r_node)


def _expand_multi_paragraph(
    dom: minidom.Document,
    p_node: minidom.Element,
    items: list[tuple[str, bool]],
    match: re.Match[str],
) -> None:
    if not items:
        if _has_sibling_paragraph(p_node) and p_node.parentNode is not None:
            p_node.parentNode.removeChild(p_node)
        return

    parent = p_node.parentNode
    next_ref = p_node.nextSibling
    original_clone = p_node.cloneNode(deep=True)
    has_list_num = _has_num_pr(p_node)

    for idx, (text, is_bullet) in enumerate(items):
        target_p = p_node if idx == 0 else original_clone.cloneNode(deep=True)
        formatted = ooxml.sanitize_xml_text(text)
        if is_bullet and not has_list_num and not formatted.startswith(("\u2022 ", "- ")):
            formatted = f"\u2022 {formatted}"
        runs = _direct_runs(target_p)
        _replace_spans_in_runs(dom, runs, [match], {match.group(1): formatted})
        if idx > 0 and parent is not None:
            parent.insertBefore(target_p, next_ref)


def _substitute_xml_part(
    xml_bytes: bytes, values: dict[str, str], multi_values: dict[str, list[tuple[str, bool]]]
) -> bytes:
    if cv_docx._DOCTYPE.search(xml_bytes):
        raise ValueError("custom template XML contains forbidden DOCTYPE declaration")
    try:
        dom = minidom.parseString(xml_bytes)
    except Exception as exc:
        raise ValueError(f"unsafe or malformed XML in custom template: {exc}") from exc

    if any(dom.getElementsByTagNameNS(_W_NS, tag) for tag in _REVIEW_MARKUP):
        raise ValueError("unsupported custom template review markup")
    # Text substitution invalidates the template's saved layout, and paragraph expansion can clone it.
    for saved_break in dom.getElementsByTagNameNS(_W_NS, "lastRenderedPageBreak"):
        saved_break.parentNode.removeChild(saved_break)
    paragraphs = list(dom.getElementsByTagNameNS(_W_NS, "p"))
    for p_node in paragraphs:
        runs = _direct_runs(p_node)
        if not runs:
            continue
        full_text = "".join(txt for _r, _t, txt in runs)
        if "{{" not in full_text:
            continue
        matches = [m for m in _PLACEHOLDER_RE.finditer(full_text) if m.group(1) in _PLACEHOLDER_SET]
        if not matches:
            continue
        if len(matches) == 1 and full_text.strip() == matches[0].group(0):
            key = matches[0].group(1)
            if key in _MULTI_PLACEHOLDERS:
                _expand_multi_paragraph(dom, p_node, multi_values[key], matches[0])
                continue
            if not values.get(key) and key in _OPTIONAL_STANDALONE and _has_sibling_paragraph(p_node):
                if p_node.parentNode is not None:
                    p_node.parentNode.removeChild(p_node)
                continue
        _replace_spans_in_runs(dom, runs, matches, values)

    return dom.toxml(encoding="utf-8")


def render_custom_template_docx_bytes(template_path: Path, doc: CoverLetterFields) -> bytes:
    """Substitute ``doc`` fields into a user-supplied custom ``.docx`` template safely."""
    values: dict[str, str] = {
        "candidate_name": doc.candidate_name,
        "candidate_email": doc.candidate_email,
        "candidate_phone": doc.candidate_phone,
        "candidate_location": doc.candidate_location,
        "candidate_links": " | ".join(doc.candidate_links),
        "date": doc.date,
        "recipient_name": doc.recipient_name,
        "recipient_title": doc.recipient_title,
        "company": doc.company,
        "role": doc.role,
        "subject": doc.subject,
        "salutation": doc.salutation,
        "opening": doc.opening,
        "closing": doc.closing,
        "signoff": doc.signoff,
    }
    para_items = [(p, False) for p in doc.paragraphs if p.strip()]
    highlight_items = [(h.strip(), True) for h in doc.highlights if h.strip()]
    body_items: list[tuple[str, bool]] = []
    if doc.opening.strip():
        body_items.append((doc.opening.strip(), False))
    body_items.extend(para_items)
    body_items.extend(highlight_items)
    if doc.closing.strip():
        body_items.append((doc.closing.strip(), False))

    multi_values: dict[str, list[tuple[str, bool]]] = {
        "paragraphs": para_items,
        "highlights": highlight_items,
        "body": body_items,
    }

    entries = _read_template_entries(template_path)
    parts = {name for name, _ in entries}
    out_buf = io.BytesIO()
    with zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as out_zip:
        for name, data in entries:
            data = _prepare_template_part(name, data, parts, doc.subject or "Cover letter")
            if _WORD_TEXT_PART_RE.match(name):
                data = _substitute_xml_part(data, values, multi_values)
            out_zip.writestr(name, data)
    return out_buf.getvalue()
