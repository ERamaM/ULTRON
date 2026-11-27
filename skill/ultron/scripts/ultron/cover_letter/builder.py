"""Render cover letter drafts as Markdown or ATS-safe Word documents."""

from __future__ import annotations

import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from ultron.cover_letter import templates, truth
from ultron.cover_letter.draft import (
    CoverLetterDocument,
    load_cover_letter_draft,
    parse_json_cover_letter,
    parse_markdown_cover_letter,
)
from ultron.cv import docx_pages
from ultron.cv import docx_reader as cv_docx
from ultron.cv import docx_writer as cv_writer
from ultron.cv import extract as cv
from ultron.infra import fs, home, ooxml

__all__ = [
    "CoverLetterDocument",
    "build_cover_letter_docx",
    "load_cover_letter_draft",
    "parse_json_cover_letter",
    "parse_markdown_cover_letter",
    "render_cover_letter_markdown",
]


def render_cover_letter_markdown(doc: CoverLetterDocument) -> str:
    """Render ``doc`` cleanly back into YAML-frontmatter + Markdown prose."""
    fm_data = {
        "candidate_name": doc.candidate_name,
        "candidate_email": doc.candidate_email,
        "candidate_phone": doc.candidate_phone,
        "candidate_location": doc.candidate_location,
        "candidate_links": doc.candidate_links,
        "date": doc.date,
        "recipient_name": doc.recipient_name,
        "recipient_title": doc.recipient_title,
        "company": doc.company,
        "role": doc.role,
        "subject": doc.subject,
        "language": doc.language,
        "tone": doc.tone,
    }
    clean_fm = {k: v for k, v in fm_data.items() if v}
    yaml_str = yaml.safe_dump(clean_fm, sort_keys=False, allow_unicode=True).strip()
    parts: list[str] = ["---", yaml_str, "---", ""]
    if doc.salutation:
        parts.extend([doc.salutation, ""])
    if doc.opening:
        parts.extend([doc.opening, ""])
    for para in doc.paragraphs:
        if para.strip():
            parts.extend([para.strip(), ""])
    if doc.highlights:
        parts.extend(f"- {item.strip()}" for item in doc.highlights if item.strip())
        parts.append("")
    if doc.closing:
        parts.extend([doc.closing, ""])
    if doc.signoff:
        parts.append(doc.signoff)
    if doc.candidate_name:
        parts.append(doc.candidate_name)
    return "\n".join(parts).rstrip() + "\n"


def _default_output_stem(src: Path, doc: CoverLetterDocument) -> str:
    slug_src = doc.company or doc.role or src.stem
    cleaned = re.sub(r"[^a-zA-Z0-9._-]{1,16}", "-", slug_src.strip()).strip("-")
    return cleaned.lower() or "cover-letter"


def build_cover_letter_docx(
    from_path: Path,
    *,
    out_path: Path | None = None,
    out_md: Path | None = None,
    template: str = "modern",
    template_file: Path | None = None,
    font: str | None = None,
    force: bool = False,
    source_cv: Path | None = None,
    confirmed_facts: list[str] | None = None,
    strict: bool = False,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Build a tailored `.docx` (and optional `.md`) cover letter with optional strict truth verification."""
    if strict and source_cv is None:
        raise cv.CvExtractionError("--strict requires --source-cv", code="missing_source_cv")
    src = Path(from_path)
    doc = load_cover_letter_draft(src)
    ref = as_of or datetime.now(UTC).date()
    if not doc.date:
        doc.date = ref.isoformat()

    stem = _default_output_stem(src, doc)
    target_docx = Path(out_path) if out_path is not None else (
        home.ultron_home() / "cover-letters" / f"{stem}.docx"
    )
    target_md = Path(out_md) if out_md is not None else None

    if target_md is not None and target_docx.resolve() == target_md.resolve():
        raise cv.CvExtractionError("Word and Markdown outputs must use different paths", code="output_collision")
    if not force:
        if target_docx.exists():
            raise FileExistsError(f"output file already exists: {target_docx} (pass --force to overwrite)")
        if target_md is not None and target_md.exists():
            raise FileExistsError(f"Markdown output already exists: {target_md} (pass --force to overwrite)")

    truth_report: dict[str, Any] | None = None
    if source_cv is not None:
        truth_report = truth.verify_cover_letter_truth(
            Path(source_cv),
            src,
            confirmed_facts=confirmed_facts,
            target_company=doc.company or None,
            as_of=ref,
            document=doc,
        )
        if strict and not truth_report["passed"]:
            raise cv_writer.TruthfulnessError(truth_report)

    if template_file is not None:
        docx_bytes = templates.render_custom_template_docx_bytes(Path(template_file), doc)
        mode_used = "custom"
    else:
        docx_bytes = templates.render_bundled_docx_bytes(doc, template_name=template, font=font)
        mode_used = template.strip().lower()

    report = cv_docx.inspect_docx(docx_bytes)
    if template_file is None and (report.table_count > 0 or report.textbox_count > 0 or report.multi_column_sections > 0):
        raise ValueError("generated bundled cover letter violated single-column ATS constraints")

    # Cover-letter length is advisory, so it does not use the CV page cap.
    count = docx_pages.measure_docx_pages(docx_bytes, report=report, max_pages=sys.maxsize)
    fs.atomic_write_bytes(target_docx, docx_bytes)
    written_md: str | None = None
    if target_md is not None:
        md_bytes = render_cover_letter_markdown(doc).encode("utf-8")
        fs.atomic_write_bytes(target_md, md_bytes)
        written_md = str(target_md.resolve())

    return {
        "ok": True,
        "out_path": str(target_docx.resolve()),
        "out_md": written_md,
        "template": mode_used,
        "font": ooxml.resolve_safe_font(
            font or templates.BUNDLED_TEMPLATES.get(mode_used, {}).get("default_font", "Calibri")
        )
        if template_file is None
        else "template",
        "bytes": len(docx_bytes),
        "pages": count.pages,
        **({"warning": f"The cover letter spans {count.pages} pages ({count.source}). Aim for one page."}
           if count.pages > 1 else {}),
        "layout": {
            "pages": count.pages,
            **count.metadata(),
            "tables": report.table_count,
            "textboxes": report.textbox_count,
            "multi_column_sections": report.multi_column_sections,
            "fonts": report.fonts,
        },
        "truth": truth_report,
    }
