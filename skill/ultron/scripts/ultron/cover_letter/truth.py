"""Deterministic truthfulness verification for cover letter drafts against a source CV."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ultron.cover_letter import draft
from ultron.cv import experience as cv_experience
from ultron.cv import extract as cv
from ultron.cv import truth as cv_truth


def _extract_draft_claim_text(draft_path: Path, document: draft.CoverLetterDocument | None) -> str:
    if document is None:
        if draft_path.suffix.lower() not in {".json", ".md", ".markdown", ".txt"}:
            return cv_truth.load_cv_plain_text(draft_path)
        if draft_path.suffix.lower() != ".json":
            if draft_path.stat().st_size > cv.MAX_CV_BYTES:
                raise cv.CvExtractionError("cover letter draft is too large", code="too_long")
            raw = cv.decode_text_bytes(draft_path.read_bytes())
            if not raw.lstrip().startswith("---"):
                # Standalone verification also accepts plain text with original line structure.
                return raw
        document = draft.load_cover_letter_draft(draft_path)
    return "\n".join((document.subject, document.salutation, document.opening,
                      *document.paragraphs, *document.highlights, document.closing, document.signoff))

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
    document: draft.CoverLetterDocument | None = None,
) -> dict[str, Any]:
    """Verify a cover letter draft against the candidate's source CV (+ confirmed facts)."""
    ref = as_of or datetime.now(UTC).date()
    source_text = cv_truth.load_cv_plain_text(source_cv_path)
    claim_text = _extract_draft_claim_text(Path(draft_path), document)
    confirmed = [fact.strip() for fact in (confirmed_facts or []) if fact.strip()]
    authorized_text = source_text + ("\n" + "\n".join(confirmed) if confirmed else "")
    company_note = (target_company or "").strip()
    # Target company only authorizes company-name vocabulary in tech/entity matching, never metrics or credentials.
    tech_authorized_text = authorized_text + (f"\n{company_note}" if company_note else "")

    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []

    src_exp = cv_experience.analyze_experience(source_text, as_of=ref)
    bounds, authorized_estimate = cv_truth.authorized_timeline(source_text, confirmed, ref, src_exp)
    verified_years = round(bounds[0] / 12.0, 1)

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
    cv_truth.check_duration_claim(
        int(draft_exp["total_months"]), bounds=bounds,
        approximate=draft_exp["experience_precision"] == "approximate",
        estimate=authorized_estimate, violations=violations,
    )

    cv_truth._check_metrics(authorized_text, claim_text, bounds, violations, info)
    cv_truth.check_claim_attribution(source_text, claim_text, confirmed, ref, violations, warnings)
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
        "experience_precision": src_exp["experience_precision"],
        "authorized_experience_month_bounds": list(bounds),
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
