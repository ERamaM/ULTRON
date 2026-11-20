"""Expected salary analysis, run-posting salary mining, and profile persistence.

Deterministic CLI helpers only: no outbound LLM calls and no unrequested web fetches.
Untrusted CV and job-posting text is parsed with linear-time regexes and redacted before
returning bounded snippets.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from ultron import cv, cv_experience, home, profile_edit, profiles, runs
from ultron.core.job_identity import canonical_job_identity
from ultron.core.privacy import redact
from ultron.core.rules.term_matching import normalize_text

SalaryPeriod = Literal["annual", "monthly", "daily", "hourly"]
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


_PREFIX_CURRENCIES: dict[str, str] = {
    "€": "EUR",
    "eur": "EUR",
    "us$": "USD",
    "usd": "USD",
    "$": "USD",
    "£": "GBP",
    "gbp": "GBP",
    "chf": "CHF",
    "ca$": "CAD",
    "c$": "CAD",
    "cad": "CAD",
    "au$": "AUD",
    "a$": "AUD",
    "aud": "AUD",
    "pln": "PLN",
    "sek": "SEK",
    "nok": "NOK",
    "dkk": "DKK",
    "r$": "BRL",
    "brl": "BRL",
    "mxn": "MXN",
    "inr": "INR",
    "₹": "INR",
}

_SUFFIX_CURRENCIES: dict[str, str] = {
    "€": "EUR",
    "eur": "EUR",
    "usd": "USD",
    "£": "GBP",
    "gbp": "GBP",
    "chf": "CHF",
    "cad": "CAD",
    "aud": "AUD",
    "pln": "PLN",
    "zl": "PLN",
    "zł": "PLN",
    "sek": "SEK",
    "nok": "NOK",
    "dkk": "DKK",
    "brl": "BRL",
    "mxn": "MXN",
    "inr": "INR",
}

_NUM = r"(?:\d{1,3}(?:[.,]\d{3}){1,3}|\d{1,9}(?:[.,]\d{1,2})?)"
_PRE_TOKEN = r"(?:€|£|₹|US\$|CA\$|C\$|AU\$|A\$|R\$|\$|\b(?:EUR|USD|GBP|CHF|CAD|AUD|PLN|SEK|NOK|DKK|BRL|MXN|INR)\b)"
_SUF_TOKEN = r"(?:€|£|zł|\b(?:EUR|USD|GBP|CHF|CAD|AUD|PLN|SEK|NOK|DKK|BRL|MXN|INR|zl)\b)"
_RANGE_JOIN = r"[ \t]{0,3}(?:[-–—]{1,2}|\b(?:to|and|a|bis)\b)[ \t]{0,3}"

_PREFIX_SALARY_RE = re.compile(
    rf"(?P<curr>{_PRE_TOKEN})[ \t]{{0,3}}(?P<low>{_NUM})(?!\d)[ \t]{{0,2}}(?P<low_k>[kK])?"
    rf"(?:{_RANGE_JOIN}(?:{_PRE_TOKEN}[ \t]{{0,3}})?(?P<high>{_NUM})(?!\d)[ \t]{{0,2}}(?P<high_k>[kK])?)?",
    re.UNICODE,
)
_SUFFIX_SALARY_RE = re.compile(
    rf"(?<!\d)(?P<low>{_NUM})(?!\d)[ \t]{{0,2}}(?P<low_k>[kK])?"
    rf"(?:{_RANGE_JOIN}(?P<high>{_NUM})(?!\d)[ \t]{{0,2}}(?P<high_k>[kK])?)?"
    rf"[ \t]{{0,3}}(?P<curr>{_SUF_TOKEN})",
    re.UNICODE,
)

_SCALE_MILLION_RE = re.compile(r"^\s*(?:[mMbB]\b|(?:million|billion|millones|mio|bn|mrd)\b)", re.IGNORECASE)
_FALSE_POSITIVE_CONTEXT = (
    "funding",
    "raised",
    "series a",
    "series b",
    "series c",
    "series d",
    "seed round",
    "valuation",
    "revenue",
    "arr",
    "mrr",
    "gmv",
    "users",
    "customers",
    "downloads",
    "401k",
    "401(k)",
    "sign-on bonus",
    "signing bonus",
    "relocation",
    "stipend",
    "learning budget",
    "education budget",
    "home office",
    "equipment budget",
    "referral",
    "per share",
    "equity grant",
)
_COMPENSATION_CONTEXT = (
    "salary",
    "compensation",
    "pay range",
    "base pay",
    "base range",
    "ote",
    "remuneration",
    "salario",
    "sueldo",
    "retribucion",
    "banda salarial",
    "rango salarial",
    "gehalt",
    "salaire",
    "remuneracao",
    "remunerazione",
    "package",
    "gross",
    "bruto",
    "brutto",
)
_PERIOD_PATTERNS: tuple[tuple[SalaryPeriod, re.Pattern[str]], ...] = (
    ("hourly", re.compile(r"(?:/\s*(?:hr|hour|h)\b|\b(?:per\s+hour|an\s+hour|hourly|por\s+hora|pro\s+stunde)\b)", re.IGNORECASE)),
    ("daily", re.compile(r"(?:/\s*(?:day|d)\b|\b(?:per\s+day|a\s+day|daily|day\s+rate|por\s+dia|al\s+dia|pro\s+tag|tjm)\b)", re.IGNORECASE)),
    ("monthly", re.compile(r"(?:/\s*(?:mo|month|mes)\b|\b(?:per\s+month|a\s+month|monthly|al\s+mes|mensual|mensuales|pro\s+monat|monatlich|par\s+mois)\b)", re.IGNORECASE)),
    ("annual", re.compile(r"(?:/\s*(?:yr|year|ano)\b|\b(?:per\s+year|a\s+year|per\s+annum|p\.a\.|annually|yearly|annual|anual|bruto\s+anual|al\s+ano|por\s+ano|jahrlich|pro\s+jahr|par\s+an|annuel)\b)", re.IGNORECASE)),
)
_PERIOD_MULTIPLIER: dict[SalaryPeriod, float] = {
    "annual": 1.0,
    "monthly": 12.0,
    "daily": 220.0,
    "hourly": 2000.0,
}
_CURRENCY_BOUNDS: dict[str, tuple[float, float]] = {
    "USD": (12_000.0, 1_200_000.0),
    "EUR": (12_000.0, 1_200_000.0),
    "GBP": (12_000.0, 1_200_000.0),
    "CHF": (15_000.0, 1_200_000.0),
    "CAD": (15_000.0, 1_200_000.0),
    "AUD": (15_000.0, 1_200_000.0),
    "PLN": (40_000.0, 4_000_000.0),
    "SEK": (120_000.0, 10_000_000.0),
    "NOK": (120_000.0, 10_000_000.0),
    "DKK": (100_000.0, 8_000_000.0),
    "BRL": (30_000.0, 5_000_000.0),
    "MXN": (120_000.0, 15_000_000.0),
    "INR": (200_000.0, 100_000_000.0),
}
_US_MARKERS = (
    "united states",
    "u.s.",
    "u.s.a.",
    "u.s.a",
    "usa",
    "us only",
    "for us locations",
    "for u.s. locations",
    "california",
    "new york",
    "texas",
    "washington",
    "san francisco",
    "seattle",
    "colorado",
    "pay transparency",
)
_UK_MARKERS = ("united kingdom", "uk only", "london", "england", "scotland", "wales")
_EU_MARKERS = (
    "europe",
    "remote eu",
    "european union",
    "spain",
    "espana",
    "germany",
    "france",
    "netherlands",
    "ireland",
    "portugal",
    "italy",
    "poland",
    "cet",
    "cest",
)
_OTHER_NON_US_MARKERS = (
    "canada",
    "australia",
    "switzerland",
    "suiza",
    "schweiz",
    "brazil",
    "mexico",
    "india",
    "latam",
    "apac",
    "singapore",
    "japan",
)
_US_LOC_RE = re.compile(r"(?<!\w)(?:us|usa|u\.s\.a?\.?)(?!\w)")
_US_BODY_RE = re.compile(
    r"(?:(?<!\w)(?:usa|u\.s\.a?\.?)(?!\w)|\bremote[\s,/-]+us\b|\bus[\s,/-]+remote\b|"
    r"\(\s*us\s*\)|,\s*us\b|\bin\s+the\s+us\b|\bus[- ]based\b|"
    r"\bus\s+(?:only|employees?|roles?|candidates?|locations?)\b)"
)
_UK_LOC_RE = re.compile(r"(?<!\w)(?:uk|u\.k\.)(?!\w)")
_UK_BODY_RE = re.compile(
    r"(?:\bremote[\s,/-]+uk\b|\buk[\s,/-]+remote\b|\(\s*uk\s*\)|,\s*uk\b|\bin\s+the\s+uk\b|\buk[- ]based\b)"
)
_EU_LOC_RE = re.compile(r"(?<!\w)(?:eu|eea)(?!\w)")
_EU_BODY_RE = re.compile(
    r"(?:\bremote[\s,/-]+eu\b|\beu[\s,/-]+remote\b|\(\s*eu\s*\)|,\s*eu\b|\bin\s+the\s+eu\b|\beu[- ]based\b)"
)

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


def _parse_amount(raw: str, has_k: bool) -> float | None:
    cleaned = raw.strip()
    if not cleaned:
        return None
    if has_k:
        normalized = cleaned.replace(",", ".")
        if normalized.count(".") > 1:
            return None
        try:
            return float(normalized) * 1000.0
        except ValueError:
            return None
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3}){1,3}", cleaned):
        return float(cleaned.replace(".", "").replace(",", ""))
    if re.fullmatch(r"\d{1,9}(?:[.,]\d{1,2})?", cleaned):
        return float(cleaned.replace(",", "."))
    return None


def _sentence_window(text: str, start: int, end: int, *, radius: int = 75) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    before = text[left:start]
    for sep in ("\n", ". ", "; "):
        pos = before.rfind(sep)
        if pos != -1:
            before = before[pos + len(sep) :]
    after = text[end:right]
    for sep in ("\n", ". ", "; "):
        pos = after.find(sep)
        if pos != -1:
            after = after[:pos]
    return f"{before}{text[start:end]}{after}".strip()


def _detect_period(text: str, start: int, end: int, window: str) -> SalaryPeriod | None:
    immediate = normalize_text(text[end : min(len(text), end + 24)])
    for period, pattern in _PERIOD_PATTERNS:
        if pattern.match(immediate.lstrip()):
            return period
    folded = normalize_text(window)
    best_period: SalaryPeriod | None = None
    best_dist = 10_000
    center = len(normalize_text(text[max(0, start - 75) : start]))
    for period, pattern in _PERIOD_PATTERNS:
        for m in pattern.finditer(folded):
            dist = abs(m.start() - center)
            if dist < best_dist:
                best_dist = dist
                best_period = period
    return best_period


def _has_marker(folded: str, markers: tuple[str, ...]) -> bool:
    return any(re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", folded) for marker in markers)


def _detect_geo_hint(text: str, *, is_location_field: bool = False) -> str:
    folded = normalize_text(text)
    if (_US_LOC_RE if is_location_field else _US_BODY_RE).search(folded) or _has_marker(folded, _US_MARKERS):
        return "us"
    if (_UK_LOC_RE if is_location_field else _UK_BODY_RE).search(folded) or _has_marker(folded, _UK_MARKERS):
        return "uk"
    if (_EU_LOC_RE if is_location_field else _EU_BODY_RE).search(folded) or _has_marker(folded, _EU_MARKERS):
        return "eu"
    if _has_marker(folded, _OTHER_NON_US_MARKERS):
        return "other_non_us"
    return "unspecified"


def _build_salary_hit(
    text: str,
    match: re.Match[str],
    currency_map: dict[str, str],
) -> dict[str, Any] | None:
    currency = currency_map.get(match.group("curr").strip().lower())
    if not currency:
        return None
    if _SCALE_MILLION_RE.match(text[match.end() : match.end() + 20]):
        return None
    low_k = bool(match.group("low_k"))
    high_k = bool(match.group("high_k"))
    low_raw = _parse_amount(match.group("low"), low_k)
    if low_raw is None or low_raw <= 0:
        return None
    if not low_k and high_k and low_raw < 1000:
        low_raw *= 1000.0
    high_str = match.group("high")
    high_raw = _parse_amount(high_str, high_k) if high_str else low_raw
    if high_raw is None or high_raw < low_raw or (high_raw > low_raw * 4 and low_raw > 0):
        return None

    window = _sentence_window(text, match.start(), match.end())
    folded_win = normalize_text(window)
    if any(term in folded_win for term in _FALSE_POSITIVE_CONTEXT):
        return None
    period = _detect_period(text, match.start(), match.end(), window)
    has_context = any(term in folded_win for term in _COMPENSATION_CONTEXT)
    if period is None and not has_context:
        return None
    resolved_period: SalaryPeriod = period or "annual"
    mult = _PERIOD_MULTIPLIER[resolved_period]
    annual_low = round(low_raw * mult)
    annual_high = round(high_raw * mult)
    min_bound, max_bound = _CURRENCY_BOUNDS.get(currency, (10_000.0, 2_000_000.0))
    if annual_low < min_bound or annual_high > max_bound:
        return None

    snippet = " ".join(window.split())
    if len(snippet) > 180:
        snippet = snippet[:177] + "..."
    return {
        "currency": currency,
        "period": resolved_period,
        "low": low_raw,
        "high": high_raw,
        "annual_low": annual_low,
        "annual_high": annual_high,
        "annual_mid": round((annual_low + annual_high) / 2),
        "geo_hint": _detect_geo_hint(window),
        "snippet": str(redact(snippet)),
        "span": (match.start(), match.end()),
    }


def extract_salaries_from_text(text: str) -> list[dict[str, Any]]:
    """Extract plausible disclosed compensation ranges from posting or CV text."""
    body = str(text or "")
    if not body.strip():
        return []
    hits: list[dict[str, Any]] = []
    occupied: list[tuple[int, int]] = []
    for pattern, cmap in ((_PREFIX_SALARY_RE, _PREFIX_CURRENCIES), (_SUFFIX_SALARY_RE, _SUFFIX_CURRENCIES)):
        for match in pattern.finditer(body):
            s, e = match.span()
            if any(not (e <= os_ or s >= oe) for os_, oe in occupied):
                continue
            hit = _build_salary_hit(body, match, cmap)
            if hit is not None:
                span = hit.pop("span")
                occupied.append(span)
                hits.append(hit)
    return hits


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return round(sorted_vals[0])
    pos = (len(sorted_vals) - 1) * pct
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return round(sorted_vals[lo])
    frac = pos - lo
    return round(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def _select_runs_for_mining(run_id: str | None, profile_slug: str | None, max_runs: int) -> list[str]:
    if run_id:
        runs.load_run(run_id)  # validates id and existence
        return [run_id]
    selected: list[str] = []
    for item in runs.list_runs():
        if profile_slug and item.get("profile_slug") != profile_slug:
            continue
        if "fetch" in (item.get("stages_done") or []):
            selected.append(str(item["run_id"]))
            if len(selected) >= max_runs:
                break
    return selected


def _kept_keys_for_run(run_folder: Path) -> set[str]:
    kept: set[str] = set()
    results_path = run_folder / "results.json"
    if results_path.is_file():
        try:
            import json

            rows = json.loads(results_path.read_text(encoding="utf-8")).get("rows") or []
            for row in rows:
                url = str(row.get("apply_url") or "")
                if url:
                    kept.add(canonical_job_identity(url, source_type=str(row.get("source_type") or ""), source_key=str(row.get("source_key") or "")).identity_key)
        except (OSError, ValueError):
            # Ignore unreadable or malformed results.json and fall back to kept.jsonl below.
            pass
    for row in runs.read_jsonl(run_folder / "kept.jsonl"):
        if key := row.get("identity_key"):
            kept.add(str(key))
    return kept


def mine_run_salaries(
    run_id: str | None = None,
    *,
    profile_slug: str | None = None,
    max_runs: int = 3,
) -> dict[str, Any]:
    """Scan fetched postings in ULTRON runs for disclosed salary ranges."""
    run_ids = _select_runs_for_mining(run_id, profile_slug, max_runs)
    seen_jobs: set[str] = set()
    all_samples: list[dict[str, Any]] = []
    postings_scanned = 0

    for rid in run_ids:
        folder = runs.run_dir(rid)
        kept_keys = _kept_keys_for_run(folder)
        for row in runs.read_jsonl(folder / "postings.jsonl"):
            key = str(row.get("identity_key") or "")
            job = row.get("job") if isinstance(row.get("job"), dict) else {}
            url = str(job.get("apply_url") or "")
            dedup_id = key or url
            if not dedup_id or dedup_id in seen_jobs:
                continue
            seen_jobs.add(dedup_id)
            postings_scanned += 1
            body = str(job.get("body_text") or "")
            hits = extract_salaries_from_text(body)
            if not hits:
                continue
            best = hits[0]
            loc_blob = f"{job.get('location') or ''} {job.get('remote_text') or ''}"
            geo_hint = _detect_geo_hint(loc_blob, is_location_field=True)
            if geo_hint == "unspecified":
                geo_hint = best["geo_hint"]
            all_samples.append({
                "run_id": rid,
                "title": str(redact(str(job.get("title") or ""))),
                "company": str(redact(str(job.get("company") or ""))),
                "location": str(redact(str(job.get("location") or ""))),
                "kept": bool(key and key in kept_keys),
                "geo_hint": geo_hint,
                "currency": best["currency"],
                "period": best["period"],
                "annual_low": best["annual_low"],
                "annual_high": best["annual_high"],
                "annual_mid": best["annual_mid"],
                "snippet": best["snippet"],
            })

    by_currency: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in all_samples:
        grouped.setdefault(sample["currency"], []).append(sample)
    for currency, items in sorted(grouped.items()):
        mids = sorted(float(item["annual_mid"]) for item in items)
        lows = [float(item["annual_low"]) for item in items]
        highs = [float(item["annual_high"]) for item in items]
        us_count = sum(1 for item in items if item["geo_hint"] == "us")
        unknown_geo_count = sum(1 for item in items if item["geo_hint"] == "unspecified")
        non_us_count = sum(1 for item in items if item["geo_hint"] not in {"us", "unspecified"})
        by_currency[currency] = {
            "count": len(items),
            "kept_count": sum(1 for item in items if item["kept"]),
            "min": round(min(lows)),
            "p25": _percentile(mids, 0.25),
            "median": _percentile(mids, 0.5),
            "p75": _percentile(mids, 0.75),
            "max": round(max(highs)),
            "us_pay_transparency_count": us_count,
            "non_us_count": non_us_count,
            "unknown_geo_count": unknown_geo_count,
        }

    def _geo_sort_rank(geo_hint: str) -> int:
        if geo_hint == "us":
            return 2
        if geo_hint == "unspecified":
            return 1
        return 0

    ranked_samples = sorted(
        all_samples,
        key=lambda item: (not item["kept"], _geo_sort_rank(item["geo_hint"]), -item["annual_mid"]),
    )[:15]
    return {
        "runs_scanned": run_ids,
        "postings_scanned": postings_scanned,
        "disclosed_count": len(all_samples),
        "by_currency": by_currency,
        "samples": ranked_samples,
    }


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
        mine_run_salaries(run_id, profile_slug=profile_slug)
        if (scan_runs or run_id)
        else {"runs_scanned": [], "postings_scanned": 0, "disclosed_count": 0, "by_currency": {}, "samples": []}
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
