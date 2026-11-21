"""Deterministic ATS CV formatting, structure, and keyword-alignment audit."""

from __future__ import annotations

import itertools
import json
import os
import re
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ultron.core import presets
from ultron.core.ats.clients.occupop import occupop_job_ref
from ultron.core.discovery.url_filter import normalize_url
from ultron.core.extractors.ats_extractors import parse_ats_page
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import JobRawPage
from ultron.core.privacy import redact
from ultron.core.rules.term_matching import contains_term, normalize_text
from ultron.cv import docx_reader as cv_docx
from ultron.cv import experience as cv_experience
from ultron.cv import extract as cv
from ultron.infra import home
from ultron.infra.net import HttpError, RequestBudgetExceeded, URLRejected
from ultron.profiles import store as profiles
from ultron.stages.fetch import get_posting_page, occupop_posting_job
from ultron.stages.http_factory import client_for_run
from ultron.storage import ledger, runs

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
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]{1,64}@(?:[A-Za-z0-9-]{1,63}\.){1,6}[A-Za-z]{2,24}\b"
)
_PHONE_PATTERN = (
    r"(?<![\d$€£¥₹])(?:"
    r"\+\d{1,3}(?:[\s.-]?\(?\d{2,4}\)?){2,4}"
    r"|\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]\d{3,4}"
    r"|\d{3,4}[\s-]\d{3,4}[\s-]\d{3,4}"
    r")(?!\d)"
)
_PHONE_RE = re.compile(_PHONE_PATTERN)
_LINK_RE = re.compile(r"(?:https?://|linkedin\.com/|github\.com/)\S+", re.IGNORECASE)
_BULLET_LINE_RE = re.compile(r"^\s*(?:[-*•▪▫◦‣]|\d+[.)])\s+")
_METRIC_RE = re.compile(
    r"(?:(?<!\d)\d{1,9}(?:[.,]\d{1,4})?%|[$€£]\s{0,2}\d|(?<!\d)\d{1,6}\s{0,2}[xX]\b|\b\d{1,4}\+)"
)

_TEX_COMMENT_RE = re.compile(r"(?<!\\)%[^\n]*")
_TEX_HREF_RE = re.compile(r"\\href\{([^{}]*)\}\{([^{}]*)\}")
_TEX_SECTION_RE = re.compile(r"\\(?:sub)*section\*?\{([^{}]*)\}")
_TEX_CMD_RE = re.compile(r"\\[a-zA-Z@]+\*?(?:\[[^\]]*\])?")

_KNOWN_SKILL_TERMS = (
    "Python", "Go", "Golang", "Rust", "Java", "Scala", "TypeScript", "JavaScript", "C++", "C#", ".NET",
    "SQL", "NoSQL", "PostgreSQL", "MySQL", "MongoDB", "Redis", "Snowflake", "BigQuery", "Redshift",
    "PyTorch", "TensorFlow", "Scikit-learn", "Pandas", "Spark", "Airflow", "dbt", "Kafka", "Flink",
    "Kubernetes", "Docker", "Terraform", "Ansible", "Linux", "AWS", "GCP", "Azure",
    "FastAPI", "Django", "Flask", "React", "Next.js", "Node.js", "GraphQL", "gRPC", "REST",
    "CI/CD", "GitHub Actions", "MLOps", "LLM", "RAG", "NLP", "Computer Vision",
    "Looker", "Tableau", "Power BI", "Excel", "Salesforce", "HubSpot", "SAP",
    "IFRS", "GAAP", "HIPAA", "GDPR", "SOC 2", "ISO 27001", "Agile", "Scrum",
)
_ACRONYM_SKIP = frozenset({
    "US", "USA", "UK", "EU", "EEA", "EMEA", "LATAM", "APAC", "CET", "CEST", "UTC", "PST", "EST",
    "HR", "CEO", "CTO", "CFO", "COO", "VP", "EEO", "URL", "PDF", "FAQ", "AM", "PM", "ID", "CV", "OR", "AND",
    "THE", "FOR", "WITH", "YOU", "OUR", "ALL", "NEW", "JOB", "INC", "LTD", "LLC", "GMBH", "SL", "SA",
})
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9/+.#-]{1,7}\b")
_TECH_CANDIDATE_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{0,24}(?:[.+/#][A-Za-z0-9]{1,16}){0,2}\b")


def is_mixed_tech_token(token: str, *, allow_digit_lower: bool = False) -> bool:
    """Return True for CamelCase, punctuated tech names (Next.js, CI/CD), or optional k8s-style tokens."""
    if any(ch in token for ch in ".+/#"):
        return True
    if any(ch.isupper() for ch in token[1:]) and any(ch.islower() for ch in token):
        return True
    return allow_digit_lower and any(a.isdigit() and b.islower() for a, b in itertools.pairwise(token))


def extract_tex_text(content: bytes, *, max_chars: int = cv.MAX_CV_CHARS) -> str:
    """Best-effort linear-time plain-text extraction from an untrusted ``.tex`` CV source."""
    raw = cv._decode_text_bytes(content)
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
        text = cv._clean_text(cv._decode_text_bytes(content), cv.MAX_CV_CHARS)
        return text, 1, suffix.lstrip("."), {}
    if suffix == cv.DOCX_SUFFIX:
        report = cv_docx.inspect_docx(content, max_chars=cv_docx.MAX_RAW_TEXT_CHARS)
        if report.pages > cv.MAX_CV_PAGES:
            raise cv.CvPdfTooManyPages(cv.MAX_CV_PAGES)
        clean = cv._clean_text(report.text, cv.MAX_CV_CHARS)
        meta = asdict(report)
        meta.pop("text", None)
        return clean, report.pages, "docx", meta

    pages, meta = cv.subprocess_pdf_inspector(
        content, max_pages=cv.MAX_CV_PAGES, max_chars=cv.MAX_CV_CHARS, timeout_seconds=cv.PDF_TIMEOUT_SECONDS
    )
    clean = cv._clean_text("\n".join(pages), cv.MAX_CV_CHARS)
    return clean, len(pages), "pdf", meta


def _write_extracted_text(source: Path, text: str) -> Path:
    out_dir = home.ultron_home() / "cv"
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{source.name}.txt"
    target.write_text(text, encoding="utf-8", newline="\n")
    return target


def _finding(
    fid: str,
    severity: str,
    category: str,
    message: str,
    recommendation: str,
    evidence: str = "",
) -> dict[str, str]:
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
        _finding(
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
                _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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
            _finding(
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


def _audit_format(file_type: str, pages: int, meta: dict[str, Any]) -> tuple[str, int, list[dict[str, str]]]:
    if file_type == "tex":
        return _audit_tex_format()
    if file_type == "pdf":
        return _audit_pdf_format(pages, meta)
    if file_type == "docx":
        return _audit_docx_format(meta)
    findings = [
        _finding(
            "plain_text_draft",
            "info",
            "format",
            "Plain text / Markdown is clean for content and keyword review, but job portals usually expect .docx.",
            "Generate an ATS-preferred clean single-column Word CV with `ultron cv build-docx`.",
        )
    ]
    return "plain_text", 24, findings


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
        findings.append(_finding("missing_email", "high", "structure", "No email address found in main body text.", "Add a professional email in the top contact block of the document body."))
    if has_phone or has_link:
        score += 2
    else:
        findings.append(_finding("missing_contact_links", "low", "structure", "No phone number or LinkedIn/GitHub link found in body text.", "Include phone and relevant profile URLs in the top contact block."))

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
            findings.append(_finding(fid, sev, "structure", msg, rec))

    if roles_count == 0:
        score = max(0, score - 4)
        findings.append(_finding("no_date_ranges_parsed", "medium", "structure", "No explicit role date ranges (e.g. `Jan 2021 - Present`) were parsed.", "Use standard `Mon YYYY - Mon YYYY` or `YYYY-MM - Present` dates on every role."))
    elif not chronology_ok:
        score = max(0, score - 3)
        findings.append(_finding("chronology_not_reverse", "medium", "structure", f"Experience entries appear in {exp.get('chronology_order')} order instead of reverse-chronological.", "Order experience newest-first so ATS parsers prioritize recent roles."))

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
        findings.append(_finding("few_bullets", "medium", "content", f"Only {bullets} bullet line(s) detected.", "Break role responsibilities and achievements into scannable bullet points."))
    if quantified < 2:
        score -= 4
        findings.append(_finding("low_quantification", "low", "content", f"Only {quantified} line(s) contain quantified metrics (%, $, scale).", "Quantify verified outcomes where real numbers exist without inventing figures."))
    if long_walls > 0:
        score -= 4
        findings.append(_finding("long_paragraph_walls", "medium", "content", f"Found {long_walls} dense paragraph(s) over 450 characters.", "Split long paragraphs into concise 1-2 line bullets."))
    if decorative > 0:
        score -= 4
        findings.append(_finding("decorative_glyphs", "medium", "content", f"Found {decorative} decorative rating/star/icon glyph(s).", "Replace visual skill bars or star ratings with explicit text proficiency."))
    if (pages > 2 and total_years < 8.0) or pages > 3:
        score -= 3
        findings.append(_finding("page_length_excessive", "low", "content", f"CV spans {pages} pages for {total_years} years of experience.", "Aim for 1 page (<5 years) or 2 pages (senior/staff)."))

    stats = {
        "bullet_lines": bullets,
        "quantified_lines": quantified,
        "long_paragraphs": long_walls,
        "decorative_glyphs": decorative,
    }
    return max(0, score), findings, stats


def extract_job_keywords(title: str, body: str, *, profile: dict | None = None) -> list[str]:
    """Extract bounded high-signal target keywords from a job title and description."""
    blob = f"{title}\n{body}"
    found: list[str] = []
    seen_low: set[str] = set()

    def add(term: str) -> None:
        clean = " ".join(term.split()).strip(".,;:()[]{}")
        if not clean or len(clean) < 2:
            return
        key = normalize_text(clean)
        if key in seen_low or clean.upper() in _ACRONYM_SKIP:
            return
        seen_low.add(key)
        found.append(clean)

    if isinstance(profile, dict):
        criteria = profile.get("criteria") if isinstance(profile.get("criteria"), dict) else {}
        qt = profile.get("query_templates") if isinstance(profile.get("query_templates"), dict) else {}
        for field in (
            criteria.get("target_titles"),
            criteria.get("role_focus_terms"),
            criteria.get("specialties"),
            criteria.get("role_signal_terms"),
            qt.get("technologies"),
            qt.get("specialties"),
        ):
            for term in field or []:
                if isinstance(term, str) and contains_term(blob, term):
                    add(term)

    for term in _KNOWN_SKILL_TERMS:
        if term == "Go":
            if re.search(r"\bGo\b", blob) or contains_term(blob, "golang"):
                add(term)
        elif contains_term(blob, term):
            add(term)

    for token in _ACRONYM_RE.findall(blob):
        if token not in _ACRONYM_SKIP and not token.isdigit():
            add(token)

    for token in _TECH_CANDIDATE_RE.findall(blob):
        if is_mixed_tech_token(token):
            add(token)

    if title.strip():
        add(title.strip())
    return found[:35]


def _profile_target_terms(profile: dict) -> list[str]:
    criteria = profile.get("criteria") if isinstance(profile.get("criteria"), dict) else {}
    qt = profile.get("query_templates") if isinstance(profile.get("query_templates"), dict) else {}
    raw: list[str] = []
    for bucket in (
        criteria.get("target_titles"),
        criteria.get("role_focus_terms"),
        criteria.get("specialties"),
        criteria.get("role_signal_terms"),
        qt.get("technologies"),
        qt.get("specialties"),
    ):
        for term in bucket or []:
            if isinstance(term, str) and term.strip():
                raw.append(term.strip())
    seen: set[str] = set()
    out: list[str] = []
    for term in raw:
        key = normalize_text(term)
        if key not in seen:
            seen.add(key)
            out.append(term)
    return out[:40]


def _find_local_job(job_arg: str, *, run_id: str | None = None) -> dict[str, Any] | None:
    ident_key = job_arg
    norm_arg = ""
    if job_arg.startswith(("http://", "https://")):
        norm_arg = normalize_url(job_arg)
        ident_key = canonical_job_identity(job_arg).identity_key

    candidate_runs: list[str] = []
    if run_id:
        runs.load_run(run_id)
        candidate_runs.append(run_id)
    else:
        db_path = home.ultron_home() / "ultron.sqlite3"
        if db_path.is_file():
            with closing(ledger.connect()) as conn:
                row = conn.execute(
                    "SELECT last_seen_run_id FROM jobs WHERE identity_key = ? OR canonical_url = ? LIMIT 1",
                    (ident_key, norm_arg or job_arg),
                ).fetchone()
                if row and row[0]:
                    candidate_runs.append(str(row[0]))
        for item in runs.list_runs():
            rid = str(item.get("run_id") or "")
            if rid and rid not in candidate_runs and "fetch" in (item.get("stages_done") or []):
                candidate_runs.append(rid)
                if len(candidate_runs) >= 6:
                    break

    for rid in candidate_runs:
        folder = runs.run_dir(rid)
        for row in runs.read_jsonl(folder / "postings.jsonl"):
            job = row.get("job") if isinstance(row.get("job"), dict) else {}
            url = str(job.get("apply_url") or "")
            row_key = str(row.get("identity_key") or "")
            url_ident = canonical_job_identity(url).identity_key if url else ""
            if job_arg == row_key or ident_key in {row_key, url_ident} or (norm_arg and url and normalize_url(url) == norm_arg):
                return {"run_id": rid, "identity_key": row_key or url_ident, **job}
    return None


def _fetch_job_url(url: str, *, profile_slug: str | None) -> dict[str, Any]:
    client = client_for_run({"settings": presets.resolve("balanced"), "profile_slug": profile_slug or "ats-audit"})
    try:
        status, final_url, html = get_posting_page(client, url)
    except (HttpError, URLRejected, RequestBudgetExceeded) as exc:
        raise ValueError(f"cannot fetch job URL ({type(exc).__name__}: {exc})") from exc
    if status >= 400:
        raise ValueError(f"job URL returned HTTP {status}")
    if occupop_job_ref(url):
        job, reason = occupop_posting_job(url, html)
        if job is None:
            raise ValueError(f"cannot extract Occupop job: {reason}")
    else:
        page = JobRawPage(url=url, final_url=final_url, status_code=status, html=html)
        job = parse_ats_page(page, ats_hints={})
    return {
        "identity_key": canonical_job_identity(job.apply_url or url).identity_key,
        "title": job.title,
        "company": job.company,
        "apply_url": job.apply_url or final_url,
        "body_text": job.body_text,
    }


def _run_aggregate_terms(run_id: str) -> tuple[list[str], dict[str, Any]]:
    runs.load_run(run_id)
    folder = runs.run_dir(run_id)
    counter: Counter[str] = Counter()
    results_path = folder / "results.json"
    rows: list[dict[str, Any]] = []
    if results_path.is_file():
        rows = json.loads(results_path.read_text(encoding="utf-8")).get("rows") or []
    for row in rows:
        for sig in row.get("fit_signals") or []:
            if isinstance(sig, str) and sig.strip():
                counter[sig.strip()] += 1
        for req in row.get("matched_requirements") or []:
            if isinstance(req, str) and req.strip():
                counter[req.strip()] += 1
    kept_rows: list[dict[str, Any]] = []
    if not counter:
        kept_rows = list(runs.read_jsonl(folder / "kept.jsonl"))
        for row in kept_rows:
            ev = row.get("heuristic") or row.get("evaluation") or {}
            if not isinstance(ev, dict):
                continue
            for field in ("fit_signals", "matched_requirements"):
                for item in ev.get(field) or []:
                    if isinstance(item, str) and item.strip():
                        counter[item.strip()] += 1
    terms = [term for term, _count in counter.most_common(30)]
    return terms, {"run_id": run_id, "kept_rows_analyzed": len(rows) or len(kept_rows)}


def _resolve_target(
    *,
    profile: dict | None,
    profile_slug: str | None,
    job_arg: str | None,
    job_file: Path | None,
    run_id: str | None,
    fetch: bool,
) -> tuple[str, list[str], dict[str, Any]]:
    if job_file is not None:
        if not job_file.is_file():
            raise FileNotFoundError(f"job file not found: {job_file}")
        if job_file.stat().st_size > cv.MAX_CV_BYTES:
            raise ValueError("job file is too large")
        raw = cv._clean_text(cv._decode_text_bytes(job_file.read_bytes()), cv.MAX_CV_CHARS)
        first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        terms = extract_job_keywords(first_line, raw, profile=profile)
        return "job", terms, {"source": "job_file", "title": str(redact(first_line)), "path": str(job_file)}

    if job_arg:
        job_data = _find_local_job(job_arg, run_id=run_id)
        source = "local_run"
        if job_data is None:
            if not job_arg.startswith(("http://", "https://")):
                raise ValueError(f"job {job_arg!r} not found in local runs")
            if not fetch:
                raise ValueError("job not found in local runs and --no-fetch was set")
            job_data = _fetch_job_url(job_arg, profile_slug=profile_slug)
            source = "fetched_url"
        title = str(job_data.get("title") or "")
        body = str(job_data.get("body_text") or "")
        terms = extract_job_keywords(title, body, profile=profile)
        return "job", terms, {
            "source": source,
            "identity_key": job_data.get("identity_key"),
            "title": str(redact(title)),
            "company": str(redact(str(job_data.get("company") or ""))),
            "apply_url": job_data.get("apply_url"),
        }

    if run_id and not profile_slug:
        terms, meta = _run_aggregate_terms(run_id)
        return "run_aggregate", terms, meta

    if profile is not None:
        terms = _profile_target_terms(profile)
        if run_id:
            agg_terms, _meta = _run_aggregate_terms(run_id)
            for term in agg_terms:
                if normalize_text(term) not in {normalize_text(t) for t in terms}:
                    terms.append(term)
        return "profile", terms[:40], {"profile": profile_slug, "run_id": run_id}

    return "general", [], {}


def _audit_target_alignment(text: str, target_terms: list[str]) -> tuple[int | None, dict[str, Any]]:
    if not target_terms:
        return None, {"terms_checked": 0, "matched": [], "missing": [], "coverage_pct": None}
    matched = [term for term in target_terms if contains_term(text, term)]
    missing = [term for term in target_terms if term not in matched]
    pct = round(100.0 * len(matched) / len(target_terms), 1)
    score = round(20.0 * len(matched) / len(target_terms))
    return score, {
        "terms_checked": len(target_terms),
        "matched": matched,
        "missing": missing,
        "coverage_pct": pct,
    }


def render_audit_markdown(audit: dict[str, Any]) -> str:
    """Render an ATS audit payload into a clean Markdown report."""
    lines = [
        "# ATS CV Audit Report",
        "",
        f"- **Source file:** `{audit['source_file']}` (`{audit['file_type']}`, {audit['pages']} page(s))",
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
    lines.extend([
        "",
        "## Parsed Experience Summary",
        "",
        f"- Total non-internship work experience: **{exp.get('total_years', 0.0)} years** ({exp.get('total_months', 0)} months)",
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
    return "\n".join(lines) + "\n"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


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
    text, pages, file_type, format_meta = _load_cv_with_inspection(source)
    extracted_path = _write_extracted_text(source, text)

    profile = profiles.load_profile(profile_slug) if profile_slug else None
    ref = as_of or datetime.now(UTC).date()
    exp = cv_experience.analyze_experience(text, profile=profile, as_of=ref)

    target_mode, target_terms, target_meta = _resolve_target(
        profile=profile,
        profile_slug=profile_slug,
        job_arg=job_arg,
        job_file=job_file,
        run_id=run_id,
        fetch=fetch,
    )

    format_tier, format_score, format_findings = _audit_format(file_type, pages, format_meta)
    struct_score, struct_findings, struct_stats = _audit_structure(text, exp)
    content_score, c_findings, c_stats = _audit_content(
        text, pages, exp, list_paragraphs=int(format_meta.get("list_paragraph_count") or 0)
    )
    content_findings = struct_findings + c_findings
    content_stats = {**struct_stats, **c_stats}
    align_score, alignment = _audit_target_alignment(text, target_terms)

    if align_score is None:
        total_score = min(100, round((format_score + struct_score + content_score) * 100 / 80))
    else:
        total_score = min(100, format_score + struct_score + content_score + align_score)

    all_findings = format_findings + content_findings
    if alignment["missing"]:
        all_findings.append(
            _finding(
                "missing_target_keywords",
                "medium",
                "keywords",
                f"Missing {len(alignment['missing'])} of {alignment['terms_checked']} target terms.",
                "Add only terms that genuinely reflect the candidate's verified experience; list unverified gaps for the user.",
                ", ".join(alignment["missing"][:10]),
            )
        )

    payload: dict[str, Any] = {
        "source_file": str(source.resolve()),
        "extracted_text_path": str(extracted_path),
        "file_type": file_type,
        "pages": pages,
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

    if write_report or out_path is not None:
        report_target = Path(out_path) if out_path is not None else (home.ultron_home() / "cv" / f"{source.name}.ats-audit.md")
        _atomic_write_text(report_target, render_audit_markdown(payload))
        payload["report_path"] = str(report_target)

    return payload
