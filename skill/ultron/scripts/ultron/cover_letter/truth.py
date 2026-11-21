"""Deterministic truthfulness verification for cover letter drafts against a source CV."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml

from ultron.cv import experience as cv_experience
from ultron.cv import extract as cv
from ultron.cv import truth as cv_truth


def _split_frontmatter(text: str) -> tuple[dict[str, Any] | None, str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None, text
    for idx in range(1, min(len(lines), 80)):
        if lines[idx].strip() == "---":
            raw_yaml = "\n".join(lines[1:idx])
            try:
                loaded = yaml.safe_load(raw_yaml)
            except yaml.YAMLError:
                return None, text
            if isinstance(loaded, dict):
                return loaded, "\n".join(lines[idx + 1 :])
            return None, text
    return None, text


def _extract_draft_claim_text(draft_path: Path) -> tuple[str, str]:
    """Return ``(claim_text, draft_company)`` from JSON, YAML-frontmatter Markdown, or plain CV-like text."""
    src = Path(draft_path)
    if not src.is_file():
        raise FileNotFoundError(f"cover letter draft not found: {src}")
    if src.stat().st_size > cv.MAX_CV_BYTES:
        raise cv.CvExtractionError("cover letter draft is too large", code="too_long")

    suffix = src.suffix.lower()
    if suffix not in {".json", ".md", ".markdown", ".txt"}:
        return cv_truth.load_cv_plain_text(src), ""

    raw = cv._decode_text_bytes(src.read_bytes())
    if suffix == ".json":
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON cover letter draft must contain an object")
        claim_parts: list[str] = []
        for field in ("subject", "opening", "closing"):
            val = data.get(field)
            if isinstance(val, str) and val.strip():
                claim_parts.append(val.strip())
        for seq_field in ("paragraphs", "highlights", "body"):
            seq = data.get(seq_field)
            if isinstance(seq, list):
                claim_parts.extend(str(item).strip() for item in seq if str(item).strip())
            elif isinstance(seq, str) and seq.strip():
                claim_parts.append(seq.strip())
        company = str(data.get("company") or "").strip()
        if claim_parts:
            return "\n".join(claim_parts), company
        return json.dumps(data, ensure_ascii=False), company

    frontmatter, body = _split_frontmatter(raw)
    if frontmatter is None:
        return raw, ""
    parts: list[str] = []
    subject = str(frontmatter.get("subject") or "").strip()
    if subject:
        parts.append(subject)
    if body.strip():
        parts.append(body.strip())
    company = str(frontmatter.get("company") or "").strip()
    return "\n".join(parts), company


def _allowed_months(source_text: str, confirmed: list[str], ref: date) -> tuple[int, dict[str, Any]]:
    src_exp = cv_experience.analyze_experience(source_text, as_of=ref)
    allowed = int(src_exp["total_months"])
    if confirmed:
        combined_entries = cv_experience.parse_date_ranges_from_lines(
            cv_truth._work_lines_for_duration(source_text) + confirmed, as_of=ref
        )
        merged = cv_experience.merge_intervals(
            [(e.start_index, e.end_index) for e in combined_entries if not e.is_internship]
        )
        allowed = max(allowed, sum((end - start) + 1 for start, end in merged))
    return allowed, src_exp


def _spanned_verified_years(source_text: str, confirmed: list[str], ref: date, roles: list[dict[str, Any]]) -> set[str]:
    spanned: set[str] = {str(ref.year)}
    for role in roles:
        start_raw = str(role.get("start") or "")[:4]
        end_raw = str(role.get("end") or "")[:4]
        if start_raw.isdigit() and end_raw.isdigit():
            s_yr, e_yr = int(start_raw), int(end_raw)
            if 1960 <= s_yr <= e_yr <= ref.year + 1:
                spanned.update(str(yr) for yr in range(s_yr, e_yr + 1))
    if confirmed:
        for entry in cv_experience.parse_date_ranges_from_lines(confirmed, as_of=ref):
            s_yr = entry.start_index // 12
            e_yr = entry.end_index // 12
            if 1960 <= s_yr <= e_yr <= ref.year + 1:
                spanned.update(str(yr) for yr in range(s_yr, e_yr + 1))
    return spanned


def verify_cover_letter_truth(
    source_cv_path: Path,
    draft_path: Path,
    *,
    confirmed_facts: list[str] | None = None,
    target_company: str | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Verify a cover letter draft against the candidate's source CV (+ confirmed facts)."""
    ref = as_of or datetime.now(UTC).date()
    source_text = cv_truth.load_cv_plain_text(source_cv_path)
    claim_text, _draft_company = _extract_draft_claim_text(draft_path)
    confirmed = [fact.strip() for fact in (confirmed_facts or []) if fact.strip()]
    authorized_text = source_text + ("\n" + "\n".join(confirmed) if confirmed else "")
    company_note = (target_company or "").strip()
    # Target company only authorizes company-name vocabulary in tech/entity matching, never metrics or credentials.
    tech_authorized_text = authorized_text + (f"\n{company_note}" if company_note else "")

    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []

    allowed_months, src_exp = _allowed_months(source_text, confirmed, ref)
    verified_years = round(allowed_months / 12.0, 1)

    auth_years = set(cv_truth._YEAR_RE.findall(authorized_text)) | _spanned_verified_years(
        source_text, confirmed, ref, src_exp.get("roles") or []
    )
    draft_years = set(cv_truth._YEAR_RE.findall(claim_text))
    for yr in sorted(draft_years - auth_years):
        violations.append({
            "type": "invented_year",
            "severity": "high",
            "token": yr,
            "message": f"Year {yr} appears in the cover letter but not in the source CV, confirmed facts, or letter date year {ref.year}.",
        })

    draft_exp = cv_experience.analyze_experience(claim_text, as_of=ref)
    draft_months = int(draft_exp["total_months"])
    if draft_months > allowed_months + 1:
        violations.append({
            "type": "inflated_experience_duration",
            "severity": "high",
            "token": f"{draft_months}m vs {allowed_months}m",
            "message": f"Cover letter implies {draft_months} months of non-internship work vs {allowed_months} verified months.",
        })

    cv_truth._check_metrics(authorized_text, claim_text, verified_years, violations, info)
    cv_truth._check_credentials(authorized_text, claim_text, violations)
    cv_truth._check_tech_tokens(tech_authorized_text, claim_text, violations, info)

    passed = not violations
    risk_level = "high" if violations else ("medium" if warnings else "low")
    return {
        "ok": passed,
        "passed": passed,
        "risk_level": risk_level,
        "source": str(Path(source_cv_path).resolve()),
        "draft": str(Path(draft_path).resolve()),
        "target_company": company_note,
        "verified_experience_years": verified_years,
        "source_experience_months": src_exp["total_months"],
        "user_confirmed_used": confirmed,
        "violations": violations,
        "warnings": warnings,
        "info": info,
        "stats": {
            "violations_count": len(violations),
            "warnings_count": len(warnings),
            "info_count": len(info),
        },
    }
