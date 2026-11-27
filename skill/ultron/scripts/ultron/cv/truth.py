"""Deterministic truthfulness verification between a source CV and an ATS-optimized draft.

Checks that rewriting did not fabricate dates, experience duration, metrics/numbers,
degrees, certifications, or technical skills/acronyms absent from the source CV and
explicitly user-confirmed facts.
"""

from __future__ import annotations

import difflib
import json
import re
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from ultron.core.privacy import redact
from ultron.core.rules.term_matching import contains_term, normalize_text
from ultron.cv import ats as cv_ats
from ultron.cv import ats_target
from ultron.cv import experience as cv_experience
from ultron.cv import extract as cv
from ultron.cv.claim_attribution import SAFE_EXPANSION_PAIRS, ClaimScope, identify_role_scope, verify_claim_attribution

_YEAR_RE = re.compile(r"\b(19[6-9]\d|20[0-3]\d)\b")
_PHONE_LINE_RE = re.compile(
    r"(?<![\d$€£¥₹])(?:"
    r"\+\d{1,3}(?:[\s.-]?\(?\d{2,4}\)?){2,4}"
    r"|\(\d{2,4}\)[\s.-]?\d{3,4}[\s.-]\d{3,4}"
    r"|\d{3,4}[\s-]\d{3,4}[\s-]\d{3,4}"
    r")(?!\d)(?!\s*(?:EUR|USD|GBP|CHF|requests|users|clients|customers|people|engineers)\b)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EMAIL_RE = cv_ats.EMAIL_RE

_WORD_NUMBERS: dict[str, int] = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
    "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7, "ocho": 8,
    "nueve": 9, "diez": 10,
}

_WORD_SIMPLE_ALT = "|".join(sorted(_WORD_NUMBERS, key=len, reverse=True))
_DECADE_ALT = "|".join(word for word, value in _WORD_NUMBERS.items() if value >= 20)
_DIGIT_WORD_ALT = "one|two|three|four|five|six|seven|eight|nine"
_WORD_COUNT_SEPARATOR = r"[-\u2010\u2011]|[ \t]{1,3}"
_WORD_NUM_ALT = rf"(?:{_DECADE_ALT})(?:{_WORD_COUNT_SEPARATOR})(?:{_DIGIT_WORD_ALT})|{_WORD_SIMPLE_ALT}"
_WORD_NUM_START = r"(?<![\w\-\u2010\u2011])"
_WORD_NUMBER_RE = re.compile(rf"{_WORD_NUM_START}({_WORD_NUM_ALT})\b", re.IGNORECASE)
_WORD_COUNT_SPLIT_RE = re.compile(r"[-\u2010\u2011 \t]+")
_CURRENCY_SYMBOL_MAP: dict[str, str] = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}

_PERCENT_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*%")
_MULT_RE = re.compile(r"\b(\d+(?:[.,]\d+)?)\s*[xX]\b")
_CURRENCY_RE = re.compile(
    r"(?:([$€£¥₹])\s*(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*([kKmMbB])?"
    r"|\b(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*([kKmMbB])?\s*(EUR|USD|GBP|CHF)\b)"
)
_YEARS_METRIC_RE = re.compile(
    r"\b(\d+(?:[.,]\d+)?)\+?\s*(?:years?|yrs?|anos?|años|ans|jahre?n?)\b",
    re.IGNORECASE,
)
_WORD_YEARS_RE = re.compile(
    rf"{_WORD_NUM_START}({_WORD_NUM_ALT})[ \t]{{1,3}}(?:years|year|yrs|yr|años|año|anos|ano|ans|jahren|jahre|jahr)\b",
    re.IGNORECASE,
)
_SCALED_NUM_RE = re.compile(
    r"\b(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|m|b|thousand|million|billion)\b",
    re.IGNORECASE,
)
_PLUS_NUM_RE = re.compile(r"\b(\d{1,3}(?:[.,]\d{3})+|\d+(?:[.,]\d+)?)\+")
_COUNT_UNITS = (
    r"engineers?|developers?|people|users?|clients?|customers?|teams?|projects?|services?|"
    r"pipelines?|models?|countr(?:y|ies)|languages?|microservices?|endpoints?|requests?|terabytes?|gb|tb|pb|ms|seconds?"
)
_COUNT_MODIFIER = (
    rf"(?!(?:{_COUNT_UNITS}|{_WORD_SIMPLE_ALT}|and|or|of|to|for|with|from|in|at|per)\b)"
    r"[a-z][a-z-]{0,23}[ \t]{1,3}"
)
_UNIT_RE = re.compile(
    rf"\b(?:(?P<number>\d{{1,3}}(?:[.,]\d{{3}})+|\d+(?:[.,]\d+)?)(?:[ \t]{{0,3}}"
    r"(?P<scale>k|m|b|thousand|million|billion))?"
    rf"|{_WORD_NUM_START}(?P<word>{_WORD_NUM_ALT}))\+?[ \t]{{1,3}}"
    rf"(?:{_COUNT_MODIFIER}){{0,2}}(?P<unit>{_COUNT_UNITS})\b",
    re.IGNORECASE,
)

_CREDENTIAL_TAGS: dict[str, tuple[str, ...]] = {
    "degree:phd": ("phd", "ph.d", "doctorate", "doctoral", "doctor of philosophy", "doctorado", "doktor"),
    "degree:masters": ("master", "masters", "master's", "msc", "m.sc", "meng", "m.eng", "mba", "magister", "maestria"),
    "degree:bachelors": ("bachelor", "bachelors", "bachelor's", "bsc", "b.sc", "beng", "b.eng", "licenciatura", "grado"),
    "cert:aws": ("aws certified", "aws solutions architect", "aws developer"),
    "cert:azure": ("azure certified", "az-900", "az-104", "az-204", "az-305", "dp-203"),
    "cert:gcp": ("google cloud certified", "gcp certified"),
    "cert:kubernetes": ("cka", "ckad", "cks", "certified kubernetes"),
    "cert:pmp": ("pmp", "project management professional"),
    "cert:cfa": ("cfa", "chartered financial analyst"),
    "cert:cpa": ("cpa", "certified public accountant"),
    "cert:cissp": ("cissp",),
    "cert:cism": ("cism",),
    "cert:togaf": ("togaf",),
    "cert:itil": ("itil",),
    "cert:scrum": ("scrum master", "certified scrum", "psm", "csm"),
    "cert:prince2": ("prince2",),
    "cert:six_sigma": ("six sigma",),
    "cert:terraform": ("terraform associate",),
    "cert:databricks": ("databricks certified",),
}

_NON_TECH_ACRONYMS = frozenset({
    "CV", "US", "USA", "UK", "EU", "EEA", "EMEA", "LATAM", "APAC", "CET", "CEST", "UTC", "GMT", "PST", "EST",
    "AM", "PM", "ID", "HR", "PR", "CEO", "CTO", "CFO", "COO", "VP", "PDF", "DOCX", "ATS", "URL", "HTTP", "HTTPS",
    "GPA", "DOB", "N/A", "NA", "OK", "OR", "AND", "THE", "FOR", "WITH", "IN", "ON", "AT", "TO", "OF", "BY",
    "A1", "A2", "B1", "B2", "C1", "C2", "I", "II", "III", "IV", "PHD", "MSC", "BSC", "MBA", "BA", "BS", "MA", "MS",
    "SL", "SA", "INC", "LTD", "LLC", "GMBH", "AG", "BV",
    "USD", "EUR", "GBP", "CHF", "JPY", "INR", "CAD", "AUD",
})



_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9/+.#-]{1,6}\b")


def _flatten_json_cv(data: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in ("name", "headline"):
        if val := str(data.get(key) or "").strip():
            lines.append(val)
    contact = data.get("contact")
    if isinstance(contact, list):
        lines.append(" | ".join(str(v).strip() for v in contact if str(v).strip()))
    elif isinstance(contact, dict):
        lines.append(" | ".join(str(v).strip() for v in contact.values() if str(v).strip()))
    elif isinstance(contact, str) and contact.strip():
        lines.append(contact.strip())
    for sec in data.get("sections") or []:
        if not isinstance(sec, dict):
            continue
        if heading := str(sec.get("heading") or "").strip():
            lines.append(heading)
        for block in sec.get("blocks") or []:
            if isinstance(block, str) and block.strip():
                lines.append(block.strip())
            elif isinstance(block, dict):
                if text := str(block.get("text") or "").strip():
                    lines.append(text)
                for item in block.get("items") or []:
                    if str(item).strip():
                        lines.append(f"- {str(item).strip()}")
    return cv._clean_text("\n".join(lines), cv.MAX_CV_CHARS)


def load_cv_plain_text(path: Path) -> str:
    """Read plain text from a CV file (``.docx``, ``.pdf``, ``.tex``, ``.txt``, ``.md``, or ``.json``)."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".tex":
        if path.stat().st_size > cv.MAX_CV_BYTES:
            raise cv.CvExtractionError("CV file is too large", code="too_long")
        return cv_ats.extract_tex_text(path.read_bytes(), max_chars=cv.MAX_CV_CHARS)
    if suffix == ".json":
        if path.stat().st_size > cv.MAX_CV_BYTES:
            raise cv.CvExtractionError("CV file is too large", code="too_long")
        data = json.loads(cv.decode_text_bytes(path.read_bytes()))
        if not isinstance(data, dict):
            raise ValueError("JSON CV file must contain an object")
        return _flatten_json_cv(data)
    return cv.extract_cv(path).text


def load_confirmed_facts(path: Path | None) -> list[str]:
    """Load user-confirmed extra facts from JSON or plain text."""
    if path is None:
        return []
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"confirmed facts file not found: {path}")
    if path.stat().st_size > cv.MAX_CV_BYTES:
        raise ValueError("confirmed facts file is too large")
    raw = cv.decode_text_bytes(path.read_bytes())
    if path.suffix.lower() == ".json":
        data = json.loads(raw)
        items = data.get("confirmed") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ValueError("confirmed facts JSON must be a list or {\"confirmed\": [...]}")
        return [str(item).strip() for item in items if str(item).strip()]
    return [line.lstrip("-*• ").strip() for line in raw.splitlines() if line.strip()]


def _strip_contacts_and_urls(text: str) -> str:
    cleaned = _URL_RE.sub(" ", text)
    cleaned = _EMAIL_RE.sub(" ", cleaned)
    return _PHONE_LINE_RE.sub(" ", cleaned)


def _scale_number(raw: str, suffix: str | None) -> float | None:
    cleaned = raw.strip()
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", cleaned):
        val = float(cleaned.replace(".", "").replace(",", ""))
    else:
        val = float(cleaned.replace(",", "."))
    mult = {
        "k": 1_000.0,
        "thousand": 1_000.0,
        "m": 1_000_000.0,
        "million": 1_000_000.0,
        "b": 1_000_000_000.0,
        "billion": 1_000_000_000.0,
    }.get((suffix or "").lower(), 1.0)
    return val * mult


def _metric_key(prefix: str, val: float) -> str:
    if abs(val - round(val)) < 1e-6:
        return f"{prefix}:{round(val)}"
    return f"{prefix}:{val:g}"


def _extract_metrics(text: str) -> tuple[dict[str, str], list[tuple[str, float]]]:
    """Return ``({canonical_key: raw_display}, [(raw_display, years_value)])`` for quantified metrics."""
    masked = _strip_contacts_and_urls(text)
    metrics: dict[str, str] = {}
    years_claims: list[tuple[str, float]] = []

    for match in _PERCENT_RE.finditer(masked):
        if (val := _scale_number(match.group(1), None)) is not None:
            metrics.setdefault(_metric_key("pct", val), match.group(0).strip())

    for match in _MULT_RE.finditer(masked):
        if (val := _scale_number(match.group(1), None)) is not None:
            metrics.setdefault(_metric_key("mult", val), match.group(0).strip())

    currency_spans: list[tuple[int, int]] = []
    for match in _CURRENCY_RE.finditer(masked):
        raw_num = match.group(2) or match.group(4) or ""
        suffix = match.group(3) or match.group(5)
        raw_curr = match.group(1) or match.group(6) or ""
        curr_code = _CURRENCY_SYMBOL_MAP.get(raw_curr, raw_curr.upper())
        if (val := _scale_number(raw_num, suffix)) is not None:
            metrics.setdefault(_metric_key(f"curr:{curr_code}", val), match.group(0).strip())
            currency_spans.append(match.span())

    def _overlaps_currency(span: tuple[int, int]) -> bool:
        s, e = span
        return any(not (e <= cs or s >= ce) for cs, ce in currency_spans)

    for match in _YEARS_METRIC_RE.finditer(masked):
        if (val := _scale_number(match.group(1), None)) is not None:
            years_claims.append((match.group(0).strip(), val))

    for match in _WORD_YEARS_RE.finditer(masked):
        word_val = _word_count(match.group(1))
        if word_val is not None:
            years_claims.append((match.group(0).strip(), float(word_val)))

    for match in _SCALED_NUM_RE.finditer(masked):
        if _overlaps_currency(match.span()):
            continue
        if (val := _scale_number(match.group(1), match.group(2))) is not None:
            metrics.setdefault(_metric_key("num", val), match.group(0).strip())

    for match in _PLUS_NUM_RE.finditer(masked):
        if not _overlaps_currency(match.span()) and (val := _scale_number(match.group(1), None)) is not None:
            metrics.setdefault(_metric_key("num", val), match.group(0).strip())

    for match in _UNIT_RE.finditer(masked):
        if _overlaps_currency(match.span()):
            continue
        raw_num = match.group("number")
        scale = match.group("scale")
        word = match.group("word")
        if raw_num and not scale and _YEAR_RE.fullmatch(raw_num):
            continue
        val = float(_word_count(word)) if word else _scale_number(raw_num, scale)
        if val is not None:
            unit = match.group("unit").lower()
            unit = "country" if unit == "countries" else unit if unit == "ms" else unit.removesuffix("s")
            metrics.setdefault(_metric_key(f"unit:{unit}", val), match.group(0).strip())

    return metrics, years_claims


def _word_count(word: str) -> int:
    return sum(_WORD_NUMBERS[part] for part in _WORD_COUNT_SPLIT_RE.split(word.casefold()))


def _word_number_keys(text: str) -> set[str]:
    return {f"num:{_word_count(match.group(1))}" for match in _WORD_NUMBER_RE.finditer(normalize_text(text))}


def _extract_credentials(text: str) -> set[str]:
    norm = normalize_text(text)
    return {
        tag
        for tag, terms in _CREDENTIAL_TAGS.items()
        if any(contains_term(norm, term) for term in terms)
    }


def _non_heading_body(text: str) -> str:
    kept = [
        line
        for line in text.splitlines()
        if cv_experience.match_section_heading(line) is None
    ]
    return "\n".join(kept)


def _extract_tech_tokens(text: str) -> dict[str, str]:
    """Extract technical acronyms, mixed-case tools, and known skill tokens from non-heading text."""
    body = _strip_contacts_and_urls(_non_heading_body(text))
    found: dict[str, str] = {}

    def record(tok: str) -> None:
        clean = tok.strip(".,;:()[]{}")
        if len(clean) < 2 or clean.upper() in _NON_TECH_ACRONYMS or clean.isdigit():
            return
        key = normalize_text(clean)
        found.setdefault(key, clean)

    for term in ats_target.KNOWN_SKILL_TERMS:
        if term == "Go":
            if re.search(r"\bGo\b", body) or contains_term(body, "golang"):
                record(term)
        elif contains_term(body, term):
            record(term)

    for left, right in SAFE_EXPANSION_PAIRS:
        for term in (left, right):
            if contains_term(body, term):
                record(term)

    for tok in _ACRONYM_RE.findall(body):
        record(tok)

    for tok in ats_target.TECH_CANDIDATE_RE.findall(body):
        if ats_target.is_mixed_tech_token(tok, allow_digit_lower=True):
            record(tok)

    return found


def _safe_expansion_partner(token_key: str, authorized_text: str) -> str | None:
    norm_auth = normalize_text(authorized_text)
    for left, right in SAFE_EXPANSION_PAIRS:
        if token_key == left and contains_term(norm_auth, right):
            return right
        if token_key == right and contains_term(norm_auth, left):
            return left
    return None


def _work_lines_for_duration(text: str) -> list[str]:
    sections, _headings = cv_experience.split_sections(text)
    if "experience" in sections:
        return sections["experience"]
    excluded = {"education", "certifications", "publications", "languages"}
    return [line for name, lines in sections.items() if name not in excluded for line in lines]


def authorized_timeline(
    source_text: str, confirmed: list[str], ref: date, source: dict,
) -> tuple[tuple[int, int], int]:
    """Combine source and confirmed timelines into conservative bounds and a month estimate."""
    minimum, maximum = int(source["minimum_total_months"]), int(source["maximum_total_months"])
    estimate = int(source["total_months"])
    if not confirmed:
        return (minimum, maximum), estimate
    entries = cv_experience.parse_date_ranges_from_lines(
        _work_lines_for_duration(source_text) + confirmed, as_of=ref,
    )
    work = [entry for entry in entries if not entry.is_internship]
    combined_min, combined_max = cv_experience.experience_month_bounds(work, as_of=ref)
    merged = cv_experience.merge_intervals([(entry.start_index, entry.end_index) for entry in work])
    combined_estimate = sum(end - start + 1 for start, end in merged)
    return (max(minimum, combined_min), max(maximum, combined_max)), max(estimate, combined_estimate)


def check_duration_claim(
    duration: int, *, bounds: tuple[int, int], approximate: bool,
    estimate: int, violations: list[dict[str, Any]],
) -> None:
    """Flag precise duration claims beyond what the source can establish."""
    minimum, maximum = bounds
    if duration > maximum + 1:
        violations.append({
            "type": "inflated_experience_duration",
            "severity": "high",
            "token": f"{duration}m vs {maximum}m",
            "message": f"The draft implies {duration} months of work, above the source maximum of {maximum}.",
        })
    elif minimum < maximum and duration > minimum and (not approximate or duration > estimate):
        violations.append({
            "type": "uncertain_experience_duration",
            "severity": "medium",
            "token": f"{duration}m",
            "message": f"The source supports between {minimum} and {maximum} months. Confirm the months in the draft.",
        })


def _check_years_and_experience(
    source_text: str,
    confirmed: list[str],
    authorized_text: str,
    optimized_text: str,
    ref: date,
    violations: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], int, tuple[int, int]]:
    auth_years = set(_YEAR_RE.findall(authorized_text))
    opt_years = set(_YEAR_RE.findall(optimized_text))
    for yr in sorted(opt_years - auth_years):
        violations.append({
            "type": "invented_year",
            "severity": "high",
            "token": yr,
            "message": f"Year {yr} appears in the optimized CV but not in the source CV or confirmed facts.",
        })

    src_exp = cv_experience.analyze_experience(source_text, as_of=ref)
    opt_exp = cv_experience.analyze_experience(optimized_text, as_of=ref)
    _check_role_dates(src_exp, opt_exp, confirmed, ref, violations)

    bounds, authorized_estimate = authorized_timeline(source_text, confirmed, ref, src_exp)
    opt_months = int(opt_exp["total_months"])
    delta = opt_months - int(src_exp["total_months"])
    if bounds[0] < bounds[1] or opt_months > bounds[1] + 1:
        check_duration_claim(
            opt_months, bounds=bounds, approximate=opt_exp["experience_precision"] == "approximate",
            estimate=authorized_estimate, violations=violations,
        )

    src_work_years = {
        yr
        for role in (src_exp.get("roles") or [])
        for yr in _YEAR_RE.findall(f"{role.get('start', '')} {role.get('end', '')}")
    }
    for yr in sorted(src_work_years - opt_years):
        warnings.append({
            "type": "dropped_source_year",
            "severity": "medium",
            "token": yr,
            "message": f"Source work-history year {yr} is missing from the optimized CV.",
        })

    return src_exp, opt_exp, delta, bounds


def _precise_role_dates(experience: dict[str, Any]) -> dict[ClaimScope, set[tuple[str, str]]]:
    roles: dict[ClaimScope, set[tuple[str, str]]] = {}
    for role in experience["roles"]:
        scope = identify_role_scope(role["title_hint"])
        if not scope.role:
            scope = ClaimScope(role=" ".join(re.findall(r"\w+", normalize_text(role["title_hint"]))))
        if scope.role and role["start_precision"] == role["end_precision"] == "month":
            roles.setdefault(scope, set()).add((role["start"], role["end"]))
    return roles


def _confirmed_date_scope(
    hint: str, roles: dict[ClaimScope, set[tuple[str, str]]], unique_role_only: dict[str, ClaimScope],
) -> ClaimScope | None:
    heading = normalize_text(hint)
    matching = [scope for scope in roles if scope.employer and contains_term(heading, scope.employer)]
    if len(matching) > 1:
        matching = [scope for scope in matching if contains_term(heading, scope.role)]
    if matching:
        return matching[0] if len(matching) == 1 else None
    fact_scope = identify_role_scope(hint)
    return unique_role_only.get(fact_scope.role) if fact_scope.employer else None


def _confirmed_role_dates(
    confirmed: list[str], ref: date, roles: dict[ClaimScope, set[tuple[str, str]]],
) -> None:
    title_counts = Counter(scope.role for scope in roles)
    unique_role_only = {
        scope.role: scope for scope, dates in roles.items()
        if not scope.employer and len(dates) == 1 and title_counts[scope.role] == 1
    }
    for fact in confirmed:
        for entry in cv_experience.parse_date_ranges_from_lines(fact.splitlines(), as_of=ref):
            start, end = entry.display_start or entry.start, entry.display_end or entry.end
            if start.year_only or end.year_only:
                continue
            scope = _confirmed_date_scope(entry.title_hint, roles, unique_role_only)
            if scope is not None:
                roles[scope].add((start.label(), "present" if entry.is_present else end.label()))


def _check_role_dates(
    source: dict[str, Any], draft: dict[str, Any], confirmed: list[str], ref: date,
    violations: list[dict[str, Any]],
) -> None:
    source_roles = _precise_role_dates(source)
    _confirmed_role_dates(confirmed, ref, source_roles)
    for scope, dates in _precise_role_dates(draft).items():
        if scope in source_roles and dates - source_roles[scope]:
            title = " | ".join(filter(None, (scope.role, scope.employer)))
            violations.append({
                "type": "changed_role_dates",
                "severity": "high",
                "token": title,
                "message": f"Employment dates for {title!r} differ from the source CV and confirmed facts.",
            })


def _check_metrics(
    authorized_text: str,
    optimized_text: str,
    month_bounds: tuple[int, int],
    violations: list[dict[str, Any]],
    info: list[dict[str, Any]],
) -> None:
    verified_years = round(month_bounds[0] / 12.0, 1)
    auth_metrics, auth_years_claims = _extract_metrics(authorized_text)
    count_keys = {f"num:{key.rsplit(':', 1)[-1]}" for key in auth_metrics if key.startswith("unit:")}
    auth_metric_keys = set(auth_metrics) | count_keys | _word_number_keys(authorized_text)
    auth_years_keys = {_metric_key("yrs", yrs_val) for _raw_claim, yrs_val in auth_years_claims}
    opt_metrics, opt_years_claims = _extract_metrics(optimized_text)
    for key, display in sorted(opt_metrics.items()):
        if key not in auth_metric_keys:
            violations.append({
                "type": "unverified_metric",
                "severity": "high",
                "token": display,
                "message": f"Metric {display!r} in the optimized CV is absent from the source CV and confirmed facts.",
            })

    for raw_claim, yrs_val in opt_years_claims:
        if _metric_key("yrs", yrs_val) in auth_years_keys:
            continue
        if month_bounds[0] < month_bounds[1] and month_bounds[0] < yrs_val * 12 <= month_bounds[1]:
            violations.append({
                "type": "uncertain_experience_duration",
                "severity": "medium",
                "token": raw_claim,
                "message": "The CV dates are approximate. Confirm the months before stating this tenure as exact.",
            })
        elif yrs_val <= verified_years + (0.5 if month_bounds[0] == month_bounds[1] else 0):
            info.append({
                "type": "derived_tenure_statement",
                "token": raw_claim,
                "message": f"Tenure claim {raw_claim!r} matches verified non-internship experience ({verified_years} yrs).",
            })
        else:
            violations.append({
                "type": "unverified_metric",
                "severity": "high",
                "token": raw_claim,
                "message": f"Experience claim {raw_claim!r} exceeds verified {verified_years} years and is not in source.",
            })


def _check_credentials(
    authorized_text: str,
    optimized_text: str,
    violations: list[dict[str, Any]],
) -> None:
    auth_creds = _extract_credentials(authorized_text)
    opt_creds = _extract_credentials(optimized_text)
    for cred in sorted(opt_creds - auth_creds):
        violations.append({
            "type": "unverified_credential",
            "severity": "high",
            "token": cred,
            "message": f"Degree or certification {cred!r} is absent from the source CV and confirmed facts.",
        })


def _check_tech_tokens(
    authorized_text: str,
    optimized_text: str,
    violations: list[dict[str, Any]],
    info: list[dict[str, Any]],
) -> None:
    opt_tokens = _extract_tech_tokens(optimized_text)
    norm_auth = normalize_text(authorized_text)
    for key, display in sorted(opt_tokens.items()):
        if contains_term(norm_auth, key):
            continue
        partner = _safe_expansion_partner(key, authorized_text)
        if partner is not None:
            info.append({
                "type": "safe_acronym_expansion",
                "token": display,
                "message": f"Added standard expansion/acronym {display!r} paired with {partner!r} from source CV.",
            })
        else:
            violations.append({
                "type": "unverified_term_or_entity",
                "severity": "high",
                "token": display,
                "message": f"Technical term or acronym {display!r} is absent from the source CV and confirmed facts.",
            })


def check_claim_attribution(
    source: str, draft: str, confirmed: list[str], ref: date,
    violations: list[dict[str, Any]], warnings: list[dict[str, Any]],
) -> None:
    """Apply the same measurable-claim attribution checks to a CV and a cover letter."""
    def metric_keys(sentence: str) -> set[str]:
        metrics, _tenure = _extract_metrics(sentence)
        return set(metrics) | _word_number_keys(sentence)

    unsupported, ambiguous = verify_claim_attribution(source, draft, confirmed, as_of=ref, metrics=metric_keys)
    violations.extend(unsupported)
    warnings.extend(ambiguous)


def verify_truth(
    source_path: Path,
    optimized_path: Path,
    *,
    confirmed_facts: list[str] | None = None,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Compare ``optimized_path`` against ``source_path`` (+ confirmed facts) and report truthfulness violations."""
    ref = as_of or datetime.now(UTC).date()
    source_text = load_cv_plain_text(source_path)
    optimized_text = load_cv_plain_text(optimized_path)
    confirmed = [fact.strip() for fact in (confirmed_facts or []) if fact.strip()]
    authorized_text = source_text + ("\n" + "\n".join(confirmed) if confirmed else "")

    violations: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    info: list[dict[str, Any]] = []

    src_exp, opt_exp, delta_months, bounds = _check_years_and_experience(
        source_text, confirmed, authorized_text, optimized_text, ref, violations, warnings
    )
    _check_metrics(authorized_text, optimized_text, bounds, violations, info)
    check_claim_attribution(source_text, optimized_text, confirmed, ref, violations, warnings)
    _check_credentials(authorized_text, optimized_text, violations)
    _check_tech_tokens(authorized_text, optimized_text, violations, info)

    diff_lines = list(
        difflib.unified_diff(
            source_text.splitlines(),
            optimized_text.splitlines(),
            fromfile=Path(source_path).name,
            tofile=Path(optimized_path).name,
            lineterm="",
        )
    )
    added = sum(1 for ln in diff_lines if ln.startswith("+") and not ln.startswith("+++"))
    removed = sum(1 for ln in diff_lines if ln.startswith("-") and not ln.startswith("---"))

    risk_level = "high" if violations else ("medium" if warnings else "low")
    return {
        "passed": not violations,
        "risk_level": risk_level,
        "source": str(Path(source_path).resolve()),
        "optimized": str(Path(optimized_path).resolve()),
        "user_confirmed_used": confirmed,
        "experience_delta_months": delta_months,
        "source_experience_months": src_exp["total_months"],
        "experience_precision": src_exp["experience_precision"],
        "authorized_experience_month_bounds": list(bounds),
        "optimized_experience_months": opt_exp["total_months"],
        "violations": violations,
        "warnings": warnings,
        "info": info,
        "diff_summary": {
            "lines_added": added,
            "lines_removed": removed,
            "preview": [str(redact(ln)) for ln in diff_lines[:40]],
        },
        "stats": {
            "violations_count": len(violations),
            "warnings_count": len(warnings),
            "info_count": len(info),
        },
    }
