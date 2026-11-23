"""Recognize disclosed compensation in untrusted vacancy text."""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Any, Literal

from ultron.core.privacy import redact
from ultron.core.rules.geography import COUNTRY_ALIASES, COUNTRY_TERMS
from ultron.core.rules.term_matching import normalize_text

SalaryPeriod = Literal["annual", "monthly", "daily", "hourly"]


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


_CLAUSE_SEP_RE = re.compile(
    r"(?:\n|[.;,][ \t]{1,3}|[ \t]{1,3}\(|\||[ \t]{1,3}[+&][ \t]{1,3}|\b(?:plus|with|and|including|alongside)\b)",
    re.IGNORECASE,
)


def _clause_window(text: str, start: int, end: int, *, radius: int = 75) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    before = text[left:start]
    last_sep = None
    for match in _CLAUSE_SEP_RE.finditer(before):
        last_sep = match
    if last_sep is not None:
        before = before[last_sep.end() :]
    after = text[end:right]
    if first_sep := _CLAUSE_SEP_RE.search(after):
        after = after[: first_sep.start()]
    return f"{before}{text[start:end]}{after}".strip()


def _nearest_period(fragment: str, amount_text: str) -> SalaryPeriod | None:
    folded = normalize_text(fragment)
    anchor = folded.find(normalize_text(amount_text))
    if anchor < 0:
        anchor = len(folded) // 2
    best_period: SalaryPeriod | None = None
    best_dist = 10_000
    for period, pattern in _PERIOD_PATTERNS:
        for m in pattern.finditer(folded):
            dist = abs(m.start() - anchor)
            if dist < best_dist:
                best_dist = dist
                best_period = period
    return best_period


def _detect_period(
    text: str, start: int, end: int, window: str, *, clause: str | None = None
) -> SalaryPeriod | None:
    immediate = normalize_text(text[end : min(len(text), end + 24)])
    for period, pattern in _PERIOD_PATTERNS:
        if pattern.match(immediate.lstrip()):
            return period
    amount_text = text[start:end]
    if clause and (clause_period := _nearest_period(clause, amount_text)):
        return clause_period
    return _nearest_period(window, amount_text)


def has_marker(folded: str, markers: tuple[str, ...]) -> bool:
    """Match one complete marker in already normalized text."""
    return any(re.search(rf"(?<!\w){re.escape(marker)}(?!\w)", folded) for marker in markers)


def detect_geo_hint(text: str, *, is_location_field: bool = False) -> str:
    """Infer a broad salary region from contextual text or a location field."""
    folded = normalize_text(text)
    if (_US_LOC_RE if is_location_field else _US_BODY_RE).search(folded) or has_marker(folded, _US_MARKERS):
        return "us"
    if (_UK_LOC_RE if is_location_field else _UK_BODY_RE).search(folded) or has_marker(folded, _UK_MARKERS):
        return "uk"
    if (_EU_LOC_RE if is_location_field else _EU_BODY_RE).search(folded) or has_marker(folded, _EU_MARKERS):
        return "eu"
    if has_marker(folded, _OTHER_NON_US_MARKERS):
        return "other_non_us"
    return "unspecified"


_MARKET_ALIASES = {
    **{term: term for term in COUNTRY_TERMS},
    **{alias: names[0] for names in COUNTRY_ALIASES for alias in names if alias != "us"},
    "espana": "spain", "madrid": "spain", "barcelona": "spain",
    "berlin": "germany", "munich": "germany", "munchen": "germany",
    "paris": "france", "london": "united kingdom", "dublin": "ireland",
}
_MARKET_PATTERN = re.compile(
    r"(?<!\w)(?:" + "|".join(map(re.escape, sorted(_MARKET_ALIASES, key=lambda key: (-len(key), key)))) + r")(?!\w)"
)
_BASE_PAY = re.compile(r"\b(?:base (?:salary|pay)|salario base|sueldo base)\b")
_TOTAL_PAY = re.compile(r"\b(?:total compensation|total pay|compensacion total|retribucion total|ote)\b")
_GROSS = re.compile(r"\b(?:gross|brut[oa]|brutto)\b")
_NET = re.compile(r"(?<![.\w])(?:net|net[oa]|netto)\b")


def salary_markets(text: str) -> set[str]:
    """Return explicit countries or their known aliases in a salary context."""
    return {_MARKET_ALIASES[match[0]] for match in _MARKET_PATTERN.finditer(normalize_text(text))}


_COUNTRY_LIST_JOIN = re.compile(r"[ \t,]{0,10}(?:(?:and|or|y|o|und|oder|et|ou)[ \t,]{1,8})?")


def _markets_in_disclosure(clause: str, window: str) -> set[str]:
    named = salary_markets(clause)
    folded = normalize_text(window)
    adjacent = [
        {_MARKET_ALIASES[first[0]], _MARKET_ALIASES[second[0]]}
        for first, second in pairwise(_MARKET_PATTERN.finditer(folded))
        if _COUNTRY_LIST_JOIN.fullmatch(folded[first.end():second.start()])
    ]
    for countries in adjacent:
        if countries & named:
            named.update(countries)
    for countries in reversed(adjacent):
        if countries & named:
            named.update(countries)
    return named


def _disclosure_context(clause: str, window: str) -> dict[str, Any]:
    countries = _markets_in_disclosure(clause, window)
    base, total = bool(_BASE_PAY.search(clause)), bool(_TOTAL_PAY.search(clause))
    gross, net = bool(_GROSS.search(clause)), bool(_NET.search(clause))
    return {
        "markets": sorted(countries),
        "compensation_type": ("base" if base else "total") if base != total else None,
        "gross_or_net": ("gross" if gross else "net") if gross != net else None,
    }


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
    clause = _clause_window(text, match.start(), match.end())
    folded_clause = normalize_text(clause)
    if has_marker(folded_clause, _FALSE_POSITIVE_CONTEXT):
        return None
    period = _detect_period(text, match.start(), match.end(), window, clause=clause)
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
        "geo_hint": detect_geo_hint(window),
        "clause_geo_hint": detect_geo_hint(clause),
        **_disclosure_context(folded_clause, window),
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
