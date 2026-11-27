"""Deterministic ATS CV formatting, structure, and keyword-alignment audit."""

from __future__ import annotations

import re
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ultron.core.privacy import redact
from ultron.cv import docx_pages
from ultron.cv import docx_reader as cv_docx
from ultron.cv import experience as cv_experience
from ultron.cv import extract as cv
from ultron.cv.ats_format import DECORATIVE_GLYPH_RE, audit_format, finding
from ultron.cv.ats_target import audit_target_alignment, resolve_target
from ultron.infra import fs, home
from ultron.profiles import store as profiles

EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,6}[A-Za-z]{2,24}\b"
)
_EMAIL_RE = EMAIL_RE
_PHONE_PATTERN = (
    r"(?<![\d$€£¥₹])(?:"
    r"\+\d{1,3}(?:[\s.-]?\(?\d{2,4}\)?){2,4}"
    r"|\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]\d{3,4}"
    r"|\d{3,4}[\s-]\d{3,4}[\s-]\d{3,4}"
    r")(?!\d)"
)
_PHONE_RE = re.compile(_PHONE_PATTERN)
_LINK_RE = re.compile(r"(?:https?://|linkedin\.com/|github\.com/)\S+", re.IGNORECASE)
_BULLET_LINE_RE = cv_experience.BULLET_PREFIX_RE
_METRIC_RE = re.compile(
    r"(?:(?<!\d)\d{1,9}(?:[.,]\d{1,4})?%|[$€£]\s{0,2}\d|(?<!\d)\d{1,6}\s{0,2}[xX]\b|\b\d{1,4}\+)"
)

_TEX_COMMENT_RE = re.compile(r"(?<!\\)%[^\n]*")
_TEX_HREF_RE = re.compile(r"\\href\{([^{}]*)\}\{([^{}]*)\}")
_TEX_SECTION_RE = re.compile(r"\\(?:sub)*section\*?\{([^{}]*)\}")
_TEX_CMD_RE = re.compile(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?")

def extract_tex_text(content: bytes, *, max_chars: int = cv.MAX_CV_CHARS) -> str:
    """Best-effort linear-time plain-text extraction from an untrusted ``.tex`` CV source."""
    raw = cv.decode_text_bytes(content)
    cleaned = _TEX_COMMENT_RE.sub("", raw)
    cleaned = _TEX_HREF_RE.sub(r"\2 (\1)", cleaned)
    cleaned = _TEX_SECTION_RE.sub(r"\n\1\n", cleaned)
    cleaned = cleaned.replace("\\\\", "\n")
    cleaned = _TEX_CMD_RE.sub(" ", cleaned)
    cleaned = cleaned.replace("{", " ").replace("}", " ").replace("\\&", "&").replace("\\%", "%")
    return cv._clean_text(cleaned, max_chars)


def _load_cv_with_inspection(path: Path) -> tuple[str, int, str, dict[str, Any]]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix not in cv.TEXT_SUFFIXES and suffix not in {".pdf", cv.DOCX_SUFFIX, ".tex"}:
        raise cv.CvExtractionError("Unsupported CV format. Use .docx, PDF, .txt, .md or .tex.", code="unsupported_type")
    try:
        if path.stat().st_size > cv.MAX_CV_BYTES:
            raise cv.CvExtractionError("CV file is too large", code="too_long")
        content = path.read_bytes()
    except OSError as exc:
        raise cv.CvExtractionError(f"Cannot read CV file: {exc}", code="unsupported_type") from exc

    if suffix == ".tex":
        text = extract_tex_text(content, max_chars=cv.MAX_CV_CHARS)
        return text, 1, "tex", {}
    if suffix in cv.TEXT_SUFFIXES:
        text = cv._clean_text(cv.decode_text_bytes(content), cv.MAX_CV_CHARS)
        return text, 1, suffix.lstrip("."), {}
    if suffix == cv.DOCX_SUFFIX:
        return _inspect_docx(content)

    pages, meta = cv.subprocess_pdf_inspector(
        content, max_pages=cv.MAX_CV_PAGES, max_chars=cv.MAX_CV_CHARS, timeout_seconds=cv.PDF_TIMEOUT_SECONDS
    )
    clean = cv._clean_text("\n".join(pages), cv.MAX_CV_CHARS)
    return clean, int(meta.get("page_count", len(pages))), "pdf", meta


def _inspect_docx(
    content: bytes, *, report: cv_docx.DocxStructureReport | None = None,
    page_count: docx_pages.DocxPageCount | None = None,
) -> tuple[str, int, str, dict[str, Any]]:
    if report is None:
        report = cv_docx.inspect_docx(content, max_chars=cv_docx.MAX_RAW_TEXT_CHARS)
    clean = cv._clean_text(report.text, cv.MAX_CV_CHARS)
    count = page_count or docx_pages.measure_docx_pages(content, report=report)
    meta = asdict(report)
    meta.pop("text", None)
    meta.update(count.metadata(), pages=count.pages)
    return clean, count.pages, "docx", meta


def _write_extracted_text(source: Path, text: str) -> Path:
    out_dir = home.ultron_home() / "cv"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{source.name}.txt"
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def _audit_structure(
    text: str,
    exp: dict[str, Any],
) -> tuple[int, list[dict[str, str]], dict[str, bool]]:
    findings: list[dict[str, str]] = []
    head_blob = "\n".join(text.splitlines()[:20])
    has_email = bool(_EMAIL_RE.search(head_blob) or _EMAIL_RE.search(text))
    has_phone = bool(_PHONE_RE.search(head_blob) or _PHONE_RE.search(text))
    has_link = bool(_LINK_RE.search(text))

    sections = set(exp.get("sections_present") or [])
    roles_count = int(exp.get("roles_count") or 0)
    chronology_ok = bool(exp.get("chronology_ok", True))

    score = 0
    if has_email:
        score += 4
    else:
        findings.append(finding("missing_email", "high", "structure", "No email address found in main body text.", "Add a professional email in the top contact block of the document body."))
    if has_phone or has_link:
        score += 2
    else:
        findings.append(finding("missing_contact_links", "low", "structure", "No phone number or LinkedIn/GitHub link found in body text.", "Include phone and relevant profile URLs in the top contact block."))

    section_rules = (
        ("experience", 7, "missing_experience_heading", "high", "No standard Experience section heading detected.", "Use a clear heading such as `Experience` or `Work Experience`."),
        ("skills", 5, "missing_skills_heading", "medium", "No standard Skills section heading detected.", "Add a dedicated `Skills` section with plain comma-separated terms."),
        ("education", 4, "missing_education_heading", "low", "No standard Education section heading detected.", "Include an `Education` section for ATS degree parsing."),
        ("summary", 3, "missing_summary_heading", "info", "No Summary/Profile section detected.", "Consider adding a concise 2-3 line Summary tailored to your target role."),
    )
    for sec_key, pts, fid, sev, msg, rec in section_rules:
        if sec_key in sections:
            score += pts
        else:
            findings.append(finding(fid, sev, "structure", msg, rec))

    if roles_count == 0:
        score = max(0, score - 4)
        findings.append(finding("no_date_ranges_parsed", "medium", "structure", "No explicit role date ranges (e.g. `Jan 2021 - Present`) were parsed.", "Use standard `Mon YYYY - Mon YYYY` or `YYYY-MM - Present` dates on every role."))
    elif not chronology_ok:
        score = max(0, score - 3)
        findings.append(finding("chronology_not_reverse", "medium", "structure", f"Experience entries appear in {exp.get('chronology_order')} order instead of reverse-chronological.", "Order experience newest-first so ATS parsers prioritize recent roles."))

    return min(25, score), findings, {"has_email": has_email, "has_phone": has_phone, "has_link": has_link}


def _audit_content(
    text: str,
    pages: int,
    exp: dict[str, Any],
    *,
    list_paragraphs: int = 0,
) -> tuple[int, list[dict[str, str]], dict[str, int]]:
    findings: list[dict[str, str]] = []
    lines = text.splitlines()
    bullets = max(sum(1 for ln in lines if _BULLET_LINE_RE.match(ln)), list_paragraphs)
    quantified = sum(1 for ln in lines if _METRIC_RE.search(ln))
    long_walls = sum(1 for ln in lines if len(ln.strip()) > 450)
    decorative = len(DECORATIVE_GLYPH_RE.findall(text))
    total_years = float(exp.get("total_years") or 0.0)

    score = 20
    if bullets < 3:
        score -= 5
        findings.append(finding("few_bullets", "medium", "content", f"Only {bullets} bullet line(s) detected.", "Break role responsibilities and achievements into scannable bullet points."))
    if quantified < 2:
        score -= 4
        findings.append(finding("low_quantification", "low", "content", f"Only {quantified} line(s) contain quantified metrics (%, $, scale).", "Quantify verified outcomes where real numbers exist without inventing figures."))
    if long_walls > 0:
        score -= 4
        findings.append(finding("long_paragraph_walls", "medium", "content", f"Found {long_walls} dense paragraph(s) over 450 characters.", "Split long paragraphs into concise 1-2 line bullets."))
    if decorative > 0:
        score -= 4
        findings.append(finding("decorative_glyphs", "medium", "content", f"Found {decorative} decorative rating/star/icon glyph(s).", "Replace visual skill bars or star ratings with explicit text proficiency."))
    if (pages > 2 and total_years < 8.0) or pages > 3:
        score -= 3
        findings.append(finding("page_length_excessive", "low", "content", f"CV spans {pages} pages for {total_years} years of experience.", "Aim for 1 page (<5 years) or 2 pages (senior/staff)."))

    stats = {
        "bullet_lines": bullets,
        "quantified_lines": quantified,
        "long_paragraphs": long_walls,
        "decorative_glyphs": decorative,
    }
    return max(0, score), findings, stats


def render_audit_markdown(audit: dict[str, Any]) -> str:
    """Render an ATS audit payload into a clean Markdown report."""
    page_source = audit.get("page_count_source")
    page_note = {
        "pdf_render": "verified via pdf_render",
        "docx_rendered_breaks": "from saved Word page breaks",
        "docx_app_props": "from saved Word document properties",
        "estimated": "estimated",
    }.get(page_source)
    page_details = f"{audit['pages']} page(s)" + (f", {page_note}" if page_note else "")
    lines = [
        "# ATS CV Audit Report",
        "",
        f"- **Source file:** `{audit['source_file']}` (`{audit['file_type']}`, {page_details})",
        f"- **Target mode:** `{audit['target']['mode']}`",
        f"- **Overall ATS score:** **{audit['score']}/100**",
        f"- **Format tier:** `{audit['format_tier']}`",
        "",
        "## Category Scores",
        "",
        f"- Format & parseability: {audit['subscores']['format_and_parseability']}/35",
        f"- Structure & sections: {audit['subscores']['structure_and_sections']}/25",
        f"- Content clarity: {audit['subscores']['content_clarity']}/20",
    ]
    if audit["subscores"]["target_alignment"] is not None:
        lines.append(f"- Target keyword alignment: {audit['subscores']['target_alignment']}/20")

    exp = audit.get("experience") or {}
    approximation = "approximately " if exp.get("experience_precision") == "approximate" else ""
    lines.extend([
        "",
        "## Parsed Experience Summary",
        "",
        f"- Total non-internship work experience: **{approximation}{exp.get('total_years', 0.0)} years** ({exp.get('total_months', 0)} months)",
        f"- Internship experience (separated): {exp.get('internship_months', 0)} months",
        f"- Inferred seniority band: `{exp.get('inferred_seniority_band', 'unknown')}`",
        f"- Chronology: `{exp.get('chronology_order', 'unknown')}`",
    ])

    findings = audit.get("findings") or []
    lines.extend(["", "## Findings & Recommendations", ""])
    if not findings:
        lines.append("- No formatting or structural issues detected.")
    else:
        for item in findings:
            ev = f" (evidence: {redact(item['evidence'])})" if item.get("evidence") else ""
            lines.append(f"- **[{item['severity'].upper()}] {item['id']}**: {item['message']}{ev}")
            lines.append(f"  - *Fix:* {item['recommendation']}")

    align = audit.get("keyword_alignment") or {}
    if align.get("terms_checked"):
        lines.extend([
            "",
            "## Target Keyword Alignment",
            "",
            f"- Coverage: **{align.get('coverage_pct')}%** ({len(align['matched'])}/{align['terms_checked']})",
            f"- Matched: {', '.join(align['matched']) or '(none)'}",
            f"- Missing (ask user before adding; never fabricate): {', '.join(align['missing']) or '(none)'}",
        ])
    criteria = align.get("criteria") or []
    if criteria:
        lines.extend(["", "## Evidence from the vacancy", ""])
        for row in criteria:
            options = " / ".join(row["alternatives"])
            state = "matched" if row["matched"] else "missing"
            lines.append(f"- **{row['category'].capitalize()} · {state}**. {options}. {row['evidence']}")

    return "\n".join(lines) + "\n"


def audit_cv(
    path: Path,
    *,
    profile_slug: str | None = None,
    job_arg: str | None = None,
    job_file: Path | None = None,
    run_id: str | None = None,
    fetch: bool = True,
    as_of: date | None = None,
    out_path: Path | None = None,
    write_report: bool = True,
) -> dict[str, Any]:
    """Audit a CV for ATS format suitability, structure, experience, and target alignment."""
    source = Path(path)
    inspected = _load_cv_with_inspection(source)
    extracted_path = _write_extracted_text(source, inspected[0])
    payload = _score_cv(
        inspected, profile_slug=profile_slug, job_arg=job_arg, job_file=job_file,
        run_id=run_id, fetch=fetch, as_of=as_of,
    )
    payload["source_file"] = str(source.resolve())
    payload["extracted_text_path"] = str(extracted_path)
    if write_report or out_path is not None:
        report_target = Path(out_path) if out_path is not None else (home.ultron_home() / "cv" / f"{source.name}.ats-audit.md")
        fs.atomic_write_text(report_target, render_audit_markdown(payload))
        payload["report_path"] = str(report_target)
    return payload


def audit_generated_docx(
    content: bytes, *, report: cv_docx.DocxStructureReport | None = None,
    page_count: docx_pages.DocxPageCount | None = None,
) -> dict[str, Any]:
    """Validate and score a generated DOCX in memory before publishing it."""
    if len(content) > cv.MAX_CV_BYTES:
        raise cv.CvExtractionError("CV file is too large", code="too_long")
    return _score_cv(_inspect_docx(content, report=report, page_count=page_count), fetch=False)


def _score_cv(
    inspected: tuple[str, int, str, dict[str, Any]],
    *,
    profile_slug: str | None = None,
    job_arg: str | None = None,
    job_file: Path | None = None,
    run_id: str | None = None,
    fetch: bool,
    as_of: date | None = None,
) -> dict[str, Any]:
    text, pages, file_type, format_meta = inspected
    profile = profiles.load_profile(profile_slug) if profile_slug else None
    ref = as_of or datetime.now(UTC).date()
    exp = cv_experience.analyze_experience(text, profile=profile, as_of=ref)

    target_mode, target_terms, target_meta = resolve_target(
        profile=profile,
        profile_slug=profile_slug,
        job_arg=job_arg,
        job_file=job_file,
        run_id=run_id,
        fetch=fetch,
    )

    format_tier, format_score, format_findings = audit_format(file_type, pages, format_meta)
    struct_score, struct_findings, struct_stats = _audit_structure(text, exp)
    content_score, c_findings, c_stats = _audit_content(
        text, pages, exp, list_paragraphs=int(format_meta.get("list_paragraph_count") or 0)
    )
    content_findings = struct_findings + c_findings
    content_stats = {**struct_stats, **c_stats}
    align_score, alignment = audit_target_alignment(text, target_terms, target_meta.pop("requirements", None))

    if align_score is None:
        total_score = min(100, round((format_score + struct_score + content_score) * 100 / 80))
    else:
        total_score = min(100, format_score + struct_score + content_score + align_score)

    all_findings = format_findings + content_findings
    if format_meta.get("page_count_source") == "estimated":
        all_findings.append(finding(
            "page_count_estimated", "info", "format", "The DOCX page count is estimated.",
            "Use LibreOffice or Microsoft Word to verify the page count with a local PDF.",
        ))
    if alignment["missing"]:
        all_findings.append(
            finding(
                "missing_target_keywords",
                "medium",
                "keywords",
                f"Missing {len(alignment['missing'])} of {alignment['terms_checked']} target terms.",
                "Add only terms that genuinely reflect the candidate's verified experience; list unverified gaps for the user.",
                ", ".join(alignment["missing"][:10]),
            )
        )

    return {
        "file_type": file_type,
        "pages": pages,
        **{key: value for key, value in format_meta.items() if key.startswith("page_count_")},
        "chars": len(text),
        "score": total_score,
        "format_tier": format_tier,
        "subscores": {
            "format_and_parseability": format_score,
            "structure_and_sections": struct_score,
            "content_clarity": content_score,
            "target_alignment": align_score,
        },
        "format_metadata": format_meta,
        "content_stats": content_stats,
        "experience": exp,
        "target": {"mode": target_mode, **target_meta},
        "keyword_alignment": alignment,
        "findings": all_findings,
    }
