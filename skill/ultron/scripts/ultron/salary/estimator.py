"""Expected salary analysis, run-posting salary mining, and profile persistence.

Deterministic CLI helpers only: no outbound LLM calls and no unrequested web fetches.
Untrusted CV and job-posting text is parsed with linear-time regexes and redacted before
returning bounded snippets.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from ultron.core.privacy import redact
from ultron.core.rules.geography import location_scope
from ultron.core.rules.term_matching import normalize_text
from ultron.cv import experience as cv_experience
from ultron.cv import extract as cv
from ultron.infra import home
from ultron.infra.fs import atomic_write_text as _atomic_write_text
from ultron.profiles import edit as profile_edit
from ultron.profiles import store as profiles
from ultron.salary.disclosure import SalaryPeriod, extract_salaries_from_text
from ultron.salary.observations import mine_run_salaries

__all__ = [
    "SalaryEstimate", "analyze_salary", "build_salary_research_queries", "extract_salaries_from_text",
    "infer_target_currencies", "mine_run_salaries", "render_salary_report_markdown", "save_salary_estimate",
]

GrossOrNet = Literal["gross", "net"]
CompensationType = Literal["base", "total"]
ConfidenceLevel = Literal["low", "medium", "high"]


class SalaryEstimate(BaseModel):
    """Structured expected salary benchmark attached to a profile or exported as a report."""

    currency: str
    period: SalaryPeriod = "annual"
    low: float
    median: float
    high: float
    gross_or_net: GrossOrNet = "gross"
    compensation_type: CompensationType = "base"
    experience_years: float | None = None
    seniority_band: str = ""
    location_market: str = ""
    confidence: ConfidenceLevel = "medium"
    methodology: str = ""
    assumptions: list[str] = Field(default_factory=list)
    sources: list[dict[str, str]] = Field(default_factory=list)
    updated_at: str = ""

    @field_validator("currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        code = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", code):
            raise ValueError("currency must be a 3-letter uppercase ISO code (e.g. EUR, USD, GBP)")
        return code

    @field_validator("low", "median", "high", "experience_years", mode="before")
    @classmethod
    def _reject_bool_amount(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("salary numeric fields must not be booleans")  # noqa: TRY004 - Pydantic wraps ValueError
        return value

    @field_validator("low", "median", "high")
    @classmethod
    def _validate_positive_amount(cls, value: float) -> float:
        num = float(value)
        if not math.isfinite(num) or num <= 0:
            raise ValueError("salary amount must be > 0")
        return num

    @field_validator("experience_years")
    @classmethod
    def _validate_experience_years(cls, value: float | None) -> float | None:
        if value is None:
            return None
        num = float(value)
        if not math.isfinite(num) or num < 0:
            raise ValueError("experience_years must be >= 0")
        return round(num, 1)

    @field_validator("sources")
    @classmethod
    def _validate_sources(cls, value: list[dict[str, str]]) -> list[dict[str, str]]:
        cleaned: list[dict[str, str]] = []
        for idx, item in enumerate(value):
            title = str(item.get("title") or "").strip()
            if not title:
                raise ValueError(f"sources[{idx}] must include a non-empty 'title'")
            entry: dict[str, str] = {"title": title}
            url = item.get("url")
            if url is not None and str(url).strip():
                raw_url = str(url).strip()
                parsed = urlsplit(raw_url)
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                    raise ValueError(f"sources[{idx}].url must be an http:// or https:// URL")
                entry["url"] = raw_url
            note = item.get("note")
            if note is not None and str(note).strip():
                entry["note"] = str(note).strip()
            cleaned.append(entry)
        return cleaned

    @field_validator("updated_at")
    @classmethod
    def _validate_updated_at(cls, value: str) -> str:
        raw = str(value or "").strip()
        if not raw:
            return datetime.now(UTC).date().isoformat()
        try:
            date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("updated_at must be an ISO date YYYY-MM-DD") from exc
        return raw

    @model_validator(mode="after")
    def _validate_ordering(self) -> SalaryEstimate:
        if self.low > self.median:
            raise ValueError("low must be <= median")
        if self.median > self.high:
            raise ValueError("median must be <= high")
        return self


_CURRENCY_GEO_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("switzerland", "suiza", "schweiz", "zurich", "geneva", "chf"), "CHF"),
    (("united kingdom", "uk", "london", "england", "scotland", "gbp"), "GBP"),
    (("poland", "warsaw", "krakow", "pln"), "PLN"),
    (("sweden", "stockholm", "sek"), "SEK"),
    (("norway", "oslo", "nok"), "NOK"),
    (("denmark", "copenhagen", "dkk"), "DKK"),
    (("canada", "toronto", "vancouver", "montreal", "cad"), "CAD"),
    (("australia", "sydney", "melbourne", "aud"), "AUD"),
    (("brazil", "brasil", "sao paulo", "brl"), "BRL"),
    (("mexico", "ciudad de mexico", "cdmx", "mxn"), "MXN"),
    (("india", "bengaluru", "bangalore", "mumbai", "inr"), "INR"),
    (
        (
            "eu",
            "europe",
            "european",
            "spain",
            "espana",
            "madrid",
            "barcelona",
            "valencia",
            "germany",
            "berlin",
            "munich",
            "france",
            "paris",
            "netherlands",
            "amsterdam",
            "ireland",
            "dublin",
            "portugal",
            "lisbon",
            "italy",
            "milan",
            "austria",
            "belgium",
            "finland",
            "eur",
        ),
        "EUR",
    ),
    (("us", "usa", "united states", "latam", "latin america", "americas", "worldwide", "global", "usd"), "USD"),
)


def infer_target_currencies(profile: dict | None, cv_text: str = "") -> list[str]:
    """Infer ordered ISO currency codes from profile location scope and CV header/text."""
    chunks: list[str] = []
    if isinstance(profile, dict):
        criteria = profile.get("criteria") if isinstance(profile.get("criteria"), dict) else {}
        loc = criteria.get("location_scope") if isinstance(criteria.get("location_scope"), dict) else {}
        qt = profile.get("query_templates") if isinstance(profile.get("query_templates"), dict) else {}
        for key in ("preferred", "eligibility_terms"):
            chunks.extend(str(v) for v in (loc.get(key) or []))
        chunks.extend(str(v) for v in (qt.get("regions") or []))
    if cv_text:
        chunks.extend(cv_text.splitlines()[:12])
    blob = normalize_text(" ".join(chunks))
    found: list[str] = []
    for keywords, currency in _CURRENCY_GEO_RULES:
        if any(re.search(rf"(?<!\w){re.escape(kw)}(?!\w)", blob) for kw in keywords) and currency not in found:
            found.append(currency)
    return found or ["EUR", "USD"]


def build_salary_research_queries(
    experience: dict[str, Any] | None,
    profile: dict | None,
    currencies: list[str],
    *,
    as_of: date,
) -> list[str]:
    """Build targeted web-research queries for live market salary benchmarking."""
    criteria = profile.get("criteria") if isinstance(profile, dict) and isinstance(profile.get("criteria"), dict) else {}
    titles = [str(t).strip() for t in (criteria.get("target_titles") or []) if str(t).strip()]
    focus = [str(t).strip() for t in (criteria.get("role_focus_terms") or []) if str(t).strip()]
    roles = (experience or {}).get("roles") or []
    recent_role = next((str(r.get("title_hint") or "").strip() for r in roles if not r.get("is_internship") and r.get("title_hint")), "")
    role = (titles[0] if titles else focus[0] if focus else recent_role or "software engineer").split("·")[0].strip()

    seniority = str((experience or {}).get("inferred_seniority_band") or "").replace("_", " ").strip()
    if seniority in {"intern entry", "staff lead"}:
        seniority = "staff" if seniority == "staff lead" else "entry level"
    role_phrase = role if (not seniority or seniority in role.lower()) else f"{seniority} {role}".strip()

    loc = criteria.get("location_scope") if isinstance(criteria.get("location_scope"), dict) else {}
    preferred = [str(r).strip() for r in (loc.get("preferred") or []) if str(r).strip()]
    market = preferred[0] if preferred else "Europe remote"
    primary_curr = currencies[0] if currencies else "EUR"
    year = as_of.year

    queries = [
        f"{role_phrase} salary {market} {year} {primary_curr}",
        f"{role_phrase} compensation range {market} {year}",
        f"{role_phrase} salary benchmark levels.fyi {market}",
    ]
    if "remote" in market.lower() or (
        isinstance(criteria.get("remote_policy"), dict) and criteria["remote_policy"].get("required") == "full_remote"
    ):
        queries.append(f"remote {role_phrase} salary {market} {year}")
    if len(preferred) > 1:
        queries.append(f"{role_phrase} salary {preferred[1]} {year}")
    return list(dict.fromkeys(" ".join(q.split()) for q in queries))


def analyze_salary(
    *,
    cv_path: Path | None = None,
    profile_slug: str | None = None,
    run_id: str | None = None,
    scan_runs: bool = True,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Extract CV experience, mine run salary disclosures, and emit research queries."""
    if cv_path is None and not profile_slug:
        raise ValueError("pass --cv <path>, --profile <slug>, or both")
    ref = as_of or datetime.now(UTC).date()
    profile = profiles.load_profile(profile_slug) if profile_slug else None
    warnings: list[str] = []

    resolved_cv: Path | None = Path(cv_path) if cv_path is not None else None
    cv_origin = "explicit" if resolved_cv is not None else None
    if resolved_cv is None and isinstance(profile, dict) and profile.get("source_cv"):
        candidate = Path(str(profile["source_cv"]))
        if candidate.is_file():
            resolved_cv = candidate
            cv_origin = "profile.source_cv"
        else:
            warnings.append("source_cv_missing")

    cv_info: dict[str, Any] | None = None
    experience: dict[str, Any] | None = None
    cv_text = ""
    if resolved_cv is not None:
        extracted = cv.extract_cv(resolved_cv)
        cv_text = extracted.text
        out_dir = home.ultron_home() / "cv"
        out_dir.mkdir(parents=True, exist_ok=True)
        txt_path = out_dir / f"{resolved_cv.name}.txt"
        txt_path.write_text(cv_text, encoding="utf-8", newline="\n")
        cv_info = {
            "source_cv": str(resolved_cv.resolve()),
            "extracted_text_path": str(txt_path),
            "chars": len(cv_text),
            "pages": extracted.pages,
            "origin": cv_origin,
        }
        experience = cv_experience.analyze_experience(cv_text, profile=profile, as_of=ref)
        warnings.extend(str(w) for w in experience.get("warnings") or [])

    currencies = infer_target_currencies(profile, cv_text)
    run_comps = (
        mine_run_salaries(run_id, profile_slug=profile_slug, target_markets=location_scope(profile)["preferred"])
        if (scan_runs or run_id)
        else {
            "runs_scanned": [], "postings_scanned": 0, "disclosed_count": 0, "by_currency": {},
            "comparable_groups": [], "target_comparable_groups": [], "samples": [], "withdrawals": [],
        }
    )
    queries = build_salary_research_queries(experience, profile, currencies, as_of=ref)

    profile_context = None
    if isinstance(profile, dict):
        criteria = profile.get("criteria") if isinstance(profile.get("criteria"), dict) else {}
        loc = criteria.get("location_scope") if isinstance(criteria.get("location_scope"), dict) else {}
        rem = criteria.get("remote_policy") if isinstance(criteria.get("remote_policy"), dict) else {}
        profile_context = {
            "slug": profile.get("slug"),
            "name": profile.get("name"),
            "target_titles": list(criteria.get("target_titles") or []),
            "role_focus_terms": list(criteria.get("role_focus_terms") or []),
            "location_preferred": list(loc.get("preferred") or []),
            "remote_required": rem.get("required"),
            "existing_expected_salary": profile.get("expected_salary"),
        }

    return {
        "as_of": ref.isoformat(),
        "cv": cv_info,
        "experience": experience,
        "profile_context": profile_context,
        "target_currencies": currencies,
        "run_market_comps": run_comps,
        "research_queries": queries,
        "warnings": list(dict.fromkeys(warnings)),
    }


def render_salary_report_markdown(estimate: SalaryEstimate, *, profile_slug: str | None = None) -> str:
    """Render a concise Markdown salary report with redacted untrusted text fields."""
    lines = ["# Expected Salary Benchmark", ""]
    if profile_slug:
        lines.append(f"- **Profile:** `{profile_slug}`")
    lines.extend([
        f"- **Updated:** {estimate.updated_at}",
        (
            f"- **Range ({estimate.period}, {estimate.gross_or_net}, {estimate.compensation_type}):** "
            f"{estimate.currency} {estimate.low:,.0f} – {estimate.high:,.0f} (median {estimate.currency} {estimate.median:,.0f})"
        ),
        f"- **Confidence:** {estimate.confidence}",
    ])
    if estimate.experience_years is not None:
        lines.append(f"- **Experience considered:** {estimate.experience_years} years")
    if estimate.seniority_band:
        lines.append(f"- **Seniority band:** {redact(estimate.seniority_band)}")
    if estimate.location_market:
        lines.append(f"- **Market / location:** {redact(estimate.location_market)}")
    if estimate.methodology:
        lines.extend(["", "## Methodology", "", str(redact(estimate.methodology))])
    if estimate.assumptions:
        lines.extend(["", "## Assumptions", ""])
        lines.extend(f"- {redact(item)}" for item in estimate.assumptions)
    if estimate.sources:
        lines.extend(["", "## Sources", ""])
        for src in estimate.sources:
            title = redact(src["title"])
            url = src.get("url")
            note = f" — {redact(src['note'])}" if src.get("note") else ""
            lines.append(f"- [{title}]({url}){note}" if url else f"- {title}{note}")
    return "\n".join(lines) + "\n"


def save_salary_estimate(
    estimate_data: dict[str, Any],
    *,
    profile_slug: str | None = None,
    dry_run: bool = False,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Validate an expected-salary payload, optionally update a profile and/or write a Markdown report."""
    estimate = SalaryEstimate.model_validate(estimate_data)
    dumped = estimate.model_dump(mode="json")
    payload: dict[str, Any] = {
        "valid": True,
        "dry_run": dry_run,
        "expected_salary": dumped,
    }
    if profile_slug:
        current = profiles.load_profile(profile_slug)
        before_text = profiles.dump_profile(current)
        updated = dict(current)
        updated["expected_salary"] = dumped
        diff = profile_edit.unified_diff(before_text, profiles.dump_profile(updated), f"{profile_slug}.yaml")
        payload["profile"] = profile_slug
        payload["changed"] = bool(diff)
        payload["diff"] = diff
        if not dry_run and diff:
            saved_path, undo_version = profiles.write_profile(updated, reason="salary")
            payload["saved"] = str(saved_path)
            payload["undo_version"] = undo_version
        else:
            payload["saved"] = None
    if report_path is not None:
        payload["report_path"] = str(report_path)
        if not dry_run:
            _atomic_write_text(report_path, render_salary_report_markdown(estimate, profile_slug=profile_slug))
    return payload
