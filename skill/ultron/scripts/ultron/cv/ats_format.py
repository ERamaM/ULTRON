"""ATS file-format audit: fonts, producers and layout signals of PDF, Word and LaTeX CVs."""

from __future__ import annotations

import re
from typing import Any

from ultron.core.privacy import redact

ATS_SAFE_FONTS = frozenset({
    "calibri",
    "arial",
    "helvetica",
    "georgia",
    "cambria",
    "garamond",
    "verdana",
    "tahoma",
    "times new roman",
    "aptos",
    "segoe ui",
    "liberation sans",
    "trebuchet ms",
})

LATEX_PRODUCER_MARKERS = (
    "pdftex",
    "xetex",
    "luatex",
    "latex",
    "tex live",
    "miktex",
    "overleaf",
    "dvipdf",
    "dvips",
    "xdvipdfmx",
    "moderncv",
    "altacv",
)

LATEX_FONT_PREFIXES = (
    "cmr",
    "cmbx",
    "cmsl",
    "cmti",
    "cmss",
    "cmtt",
    "cmmi",
    "cmsy",
    "lmroman",
    "lmsans",
    "lmmono",
    "ecrm",
    "ecbx",
    "texgyre",
    "f15",
    "sfrm",
    "sfbx",
)

DESIGN_TOOL_MARKERS = (
    "canva",
    "figma",
    "illustrator",
    "indesign",
    "photoshop",
    "inkscape",
    "coreldraw",
    "affinity",
    "sketch",
)

DECORATIVE_GLYPH_RE = re.compile(r"[★☆●○◆◇■□▪▫▶►✓✔✗✘]")


def finding(
    fid: str,
    severity: str,
    category: str,
    message: str,
    recommendation: str,
    evidence: str = "",
) -> dict[str, str]:
    """Build an ATS formatting audit finding dictionary with optional redacted evidence."""
    item = {
        "id": fid,
        "severity": severity,
        "category": category,
        "message": message,
        "recommendation": recommendation,
    }
    if evidence:
        item["evidence"] = str(redact(evidence))[:180]
    return item


def _has_safe_font(fonts: list[str]) -> bool:
    for font in fonts:
        base = font.split(",")[0].strip().lower()
        if any(base == safe or base.startswith((safe + "-", safe + " ")) for safe in ATS_SAFE_FONTS):
            return True
    return False


def _audit_tex_format() -> tuple[str, int, list[dict[str, str]]]:
    findings = [
        finding(
            "latex_source_format",
            "high",
            "format",
            "Raw LaTeX (.tex) sources cannot be parsed by standard ATS portals.",
            "Compile or rewrite into a clean single-column Word (.docx) CV using `ultron cv build-docx`; "
            "clean .docx is preferred over both .tex sources and LaTeX-compiled PDFs.",
        )
    ]
    return "unsuitable", 4, findings


def _audit_pdf_format(pages: int, meta: dict[str, Any]) -> tuple[str, int, list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    producer = str(meta.get("producer") or "")
    creator = str(meta.get("creator") or "")
    meta_blob = f"{producer} {creator}".lower()
    fonts = [str(f) for f in (meta.get("fonts") or [])]

    is_latex = any(marker in meta_blob for marker in LATEX_PRODUCER_MARKERS) or any(
        f.lower().startswith(LATEX_FONT_PREFIXES) for f in fonts
    )
    is_design = any(marker in meta_blob for marker in DESIGN_TOOL_MARKERS)
    two_col_pages = list(meta.get("two_column_pages") or [])
    rotated_pages = list(meta.get("rotated_text_pages") or [])
    images = int(meta.get("image_count") or 0)
    weird_glyphs = (
        int(meta.get("raw_ligature_count") or 0)
        + int(meta.get("private_use_glyph_count") or 0)
        + int(meta.get("replacement_char_count") or 0)
    )

    chars_per_page = [int(c) for c in (meta.get("chars_per_page") or [])]
    if chars_per_page:
        total_chars = sum(chars_per_page)
        avg_chars = total_chars / len(chars_per_page)
        if (avg_chars < 120 and images > 0) or total_chars < 80:
            findings.append(
                finding(
                    "scanned_or_sparse_pdf",
                    "high",
                    "format",
                    "PDF contains very little selectable text and appears scanned or image-based.",
                    "Recreate the CV as a text-native single-column Word (.docx) document (`ultron cv build-docx`).",
                    f"{total_chars} chars across {len(chars_per_page)} page(s), {images} image(s)",
                )
            )
            return "unsuitable", 4, findings

    score = 26
    if is_latex:
        score -= 12
        evidence = ", ".join(p for p in (producer, creator, *fonts[:3]) if p)
        findings.append(
            finding(
                "latex_pdf_detected",
                "high",
                "format",
                "LaTeX-compiled PDF detected. LaTeX PDFs frequently cause ligature, font-encoding, and "
                "layout reading-order issues in ATS parsers and are NOT preferred over Word (.docx).",
                "Convert your CV to a clean single-column Word (.docx) file (`ultron cv build-docx`).",
                evidence,
            )
        )
    if is_design:
        score -= 12
        findings.append(
            finding(
                "design_tool_pdf_detected",
                "high",
                "format",
                "Design-tool PDF detected (Canva/Figma/Illustrator/InDesign). Vector boxes often scramble ATS text order.",
                "Rebuild as a clean single-column .docx document (`ultron cv build-docx`).",
                f"{producer} {creator}".strip(),
            )
        )
    if two_col_pages:
        score -= 8
        findings.append(
            finding(
                "multi_column_layout",
                "high",
                "format",
                f"Multi-column PDF layout detected on page(s) {two_col_pages}; legacy ATS readers read across columns.",
                "Switch to a single-column layout so titles, dates, and skills never interleave.",
            )
        )
    if rotated_pages:
        score -= 4
        findings.append(
            finding(
                "rotated_text",
                "medium",
                "format",
                f"Rotated text detected on page(s) {rotated_pages}, which ATS parsers frequently drop.",
                "Keep all CV text horizontal in standard paragraphs.",
            )
        )
    if images > 0:
        score -= 4
        findings.append(
            finding(
                "embedded_images",
                "medium",
                "format",
                f"Found {images} embedded image(s); ATS cannot reliably index text or icons inside images.",
                "Remove photos, logos, and graphic skill bars.",
            )
        )
    if weird_glyphs > 0:
        score -= 4
        findings.append(
            finding(
                "non_standard_glyphs",
                "medium",
                "format",
                f"Detected {weird_glyphs} ligature, private-use, or replacement glyph(s) in raw PDF text.",
                "Export to clean .docx or use standard Unicode characters and ATS-safe fonts.",
            )
        )
    if fonts and not _has_safe_font(fonts) and not is_latex:
        score -= 2
        findings.append(
            finding(
                "non_standard_fonts",
                "low",
                "format",
                f"Non-standard fonts detected: {', '.join(fonts[:5])}.",
                "Use Calibri, Arial, Helvetica, Georgia, or Cambria.",
            )
        )

    if is_latex or is_design:
        tier = "discouraged"
    elif two_col_pages or rotated_pages or images > 0 or weird_glyphs > 0:
        tier = "risky"
    else:
        tier = "acceptable"
        findings.append(
            finding(
                "pdf_vs_docx_preference",
                "info",
                "format",
                "Clean single-column PDF parses well in modern ATS, though clean single-column Word (.docx) "
                "is preferred for maximum compatibility across older ATS portals.",
                "Keep a clean single-column .docx version ready (`ultron cv build-docx`).",
            )
        )

    if pages > 2 and score > 8:
        score -= 2
    return tier, max(4, min(35, score)), findings


def _audit_docx_format(meta: dict[str, Any]) -> tuple[str, int, list[dict[str, str]]]:
    findings: list[dict[str, str]] = []
    score = 35
    high_count = 0
    medium_count = 0

    tables = int(meta.get("table_count") or 0)
    textboxes = int(meta.get("textbox_count") or 0)
    multi_cols = int(meta.get("multi_column_sections") or 0)
    images = int(meta.get("image_count") or 0)
    hf_contact = bool(meta.get("header_footer_has_contact"))
    artifacts = bool(meta.get("has_tracked_changes") or meta.get("has_comments") or meta.get("has_hidden_text"))
    non_std = int(meta.get("non_standard_char_count") or 0)
    fonts = [str(f) for f in (meta.get("fonts") or [])]

    if tables > 0:
        high_count += 1
        score -= 8
        findings.append(
            finding(
                "tables_detected",
                "high",
                "format",
                f"Found {tables} Word table(s); layout tables often scramble reading order in ATS parsers.",
                "Replace tables with clean single-column paragraphs and bullet lists.",
            )
        )
    if textboxes > 0:
        high_count += 1
        score -= 8
        findings.append(
            finding(
                "textboxes_detected",
                "high",
                "format",
                f"Found {textboxes} text box(es); many ATS parsers ignore text inside floating drawing boxes.",
                "Move all content out of text boxes into standard body paragraphs.",
            )
        )
    if multi_cols > 0:
        high_count += 1
        score -= 8
        findings.append(
            finding(
                "multi_column_layout",
                "high",
                "format",
                "Multi-column Word section layout detected.",
                "Use a single-column page layout throughout.",
            )
        )
    if hf_contact:
        high_count += 1
        score -= 7
        findings.append(
            finding(
                "contact_in_header_footer",
                "high",
                "format",
                "Contact details appear inside Word header/footer XML; ATS parsers and ULTRON skip headers/footers.",
                "Place your name, email, phone, and profile links directly at the top of the document body.",
            )
        )
    if images > 0:
        medium_count += 1
        score -= 4
        findings.append(
            finding(
                "embedded_images",
                "medium",
                "format",
                f"Found {images} embedded image(s) in the .docx package.",
                "Remove images, headshots, and graphic icons.",
            )
        )
    if artifacts:
        medium_count += 1
        score -= 4
        findings.append(
            finding(
                "word_review_artifacts",
                "medium",
                "format",
                "Tracked changes, comments, or hidden text found in the .docx file.",
                "Accept all changes, delete comments, and remove hidden text before submitting.",
            )
        )
    if non_std > 0:
        score -= 2
        findings.append(
            finding(
                "non_standard_glyphs",
                "low",
                "format",
                f"Found {non_std} private-use, ligature, or zero-width characters in Word runs.",
                "Use standard Unicode bullets and characters.",
            )
        )
    if fonts and not _has_safe_font(fonts):
        score -= 2
        findings.append(
            finding(
                "non_standard_fonts",
                "low",
                "format",
                f"Non-standard fonts detected: {', '.join(fonts[:5])}.",
                "Use Calibri, Arial, Georgia, Cambria, or Verdana.",
            )
        )

    if high_count > 0 or medium_count > 1:
        tier = "risky"
    elif medium_count == 1:
        tier = "acceptable"
    else:
        tier = "preferred"
    return tier, max(6, score), findings


def audit_format(file_type: str, pages: int, meta: dict[str, Any]) -> tuple[str, int, list[dict[str, str]]]:
    """Evaluate CV file-format hygiene for TeX, PDF, DOCX, or text sources."""
    if file_type == "tex":
        return _audit_tex_format()
    if file_type == "pdf":
        return _audit_pdf_format(pages, meta)
    if file_type == "docx":
        return _audit_docx_format(meta)
    findings = [
        finding(
            "plain_text_draft",
            "info",
            "format",
            "Plain text / Markdown is clean for content and keyword review, but job portals usually expect .docx.",
            "Generate an ATS-preferred clean single-column Word CV with `ultron cv build-docx`.",
        )
    ]
    return "plain_text", 24, findings


