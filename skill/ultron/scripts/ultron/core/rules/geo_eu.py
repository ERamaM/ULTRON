"""European (EU/Spain) geography rules, applied only to European-scope profiles.

This is the only core module allowed to name European places. Everything here was
moved verbatim from ``rules/hard_filters.py``, ``rules/hard_filters_data.py`` and
``rules/geography.py``; the logic is MARK III's. Callers pick these rules only when
``geo_scope(profile) == "eu"`` (see ``rules/geography.py``).

This module is a leaf: it imports generic vocabulary from ``hard_filters_data`` and the
text helpers, and nothing imports back into it from those modules.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache

from ultron.core.models import Eligibility, RemotePolicy
from ultron.core.rules import hard_filters_signals as signals
from ultron.core.rules.hard_filters_data import (
    EXPLICIT_HIRING_SCOPE_TERMS,
    INCIDENTAL_GEO_TERMS,
    LOCATION_CONTEXT_TERMS,
    REMOTE_HINT_TERMS,
)
from ultron.core.rules.term_matching import normalize_text
from ultron.core.rules.text_patterns import (
    contains_any_substring,
    contains_any_term_boundary,
    contains_any_word_boundary,
    word_boundary_pattern,
)

# --- scope tokens (was rules/geography.py) ---------------------------------------------------

EUROPEAN_SCOPE_TOKENS = frozenset({"europe", "european", "europa", "eu", "ue", "eea", "emea", "spain", "espana"})

# --- data (was rules/hard_filters_data.py) ---------------------------------------------------

DISALLOWED_REGION_TERMS_WB_PHRASES = {
    "united states only",
    "canada only",
    "australia only",
    "uk only",
    "right to work in the uk",
    "uk right to work",
    "must be based in the uk",
    "north america only",
    "based in north america",
    "pt hours",
    "et hours",
}
# "est" removed: \best\b matched "Est. 2012" and French "est". It is now only
# treated as a US timezone when adjacent to timezone-ish context (see
# _has_est_timezone_signal).
DISALLOWED_REGION_TERMS_WB = {"pst", "cst", "mst"}

# "us only" variants with word boundaries plus an idiom guard ("join us only",
# "to us only", ...) handled in _has_us_only_signal.
US_ONLY_PATTERN = re.compile(r"\b(?:usa|us|u\.s\.a?\.?)[\s\-]?only\b", re.IGNORECASE)
US_ONLY_IDIOM_PRECEDERS = {"join", "to", "for", "with"}

SECURITY_CLEARANCE_PATTERN = re.compile(r"\bsecurity clearance\b", re.IGNORECASE)
SECURITY_CLEARANCE_NEGATIONS = ("no ", "without ", "not require")

# "est" only counts as a US timezone signal when adjacent to timezone context
# ("5pm EST", "EST hours", "(EST)", "EST time zone", ...).
EST_TZ_CONTEXT_PATTERN = re.compile(
    r"\(\s*est\s*\)"
    r"|\best\b[^a-z0-9]{0,4}(?:time\s*zone|timezone|time|hours|business)"
    r"|(?:time\s*zone|timezone|time|hours|am|pm|utc[+\-]?\d{0,2})[^a-z0-9]{0,4}\best\b",
    re.IGNORECASE,
)
NON_EU_LOCATION_TERMS = {
    "uk",
    "united kingdom",
    "england",
    "scotland",
    "wales",
    "london",
    "cardiff",
    "canada",
    "toronto",
    "vancouver",
    "montreal",
    "mexico",
    "mexico city",
    "japan",
    "tokyo",
    "uae",
    "united arab emirates",
    "dubai",
    "australia",
    "sydney",
    "melbourne",
    "new zealand",
    "auckland",
    "singapore",
    "china",
    "bangladesh",
    "dhaka",
    "brazil",
    "india",
    "bangalore",
    "bengaluru",
    "mumbai",
    "delhi",
    "hyderabad",
    "pakistan",
    "islamabad",
    "karachi",
    "lahore",
    "philippines",
    "manila",
    # Spanish names of the countries above (accent-folded: the text is normalized before matching).
    "brasil",
    "japon",
    "reino unido",
}
# Countries outside Europe that postings restrict remote work to ("Fully remote from Argentina only"), with
# their Spanish names. Unlike NON_EU_LOCATION_TERMS they only flag a posting that names no European scope
# at all ("Remote within the EU; founders from Israel" stays eligible).
NON_EU_SCOPED_COUNTRY_TERMS = {
    "argentina",
    "chile",
    "colombia",
    "peru",
    "uruguay",
    "vietnam",
    "south korea",
    "corea del sur",
    "israel",
    "turkey",
    "turkiye",
    "turquia",
    "saudi arabia",
    "arabia saudi",
    "arabia saudita",
    "indonesia",
    "malaysia",
    "malasia",
    "thailand",
    "tailandia",
    "nigeria",
    "kenya",
    "south africa",
    "sudafrica",
    "egypt",
    "egipto",
}
# "worldwide" keeps a posting open to anyone: "Remote worldwide, including Argentina" is not restricted.
NON_EU_SCOPED_COUNTRY_OVERRIDE_TERMS = {"emea", "europe", "europa", "eu", "ue", "eea", "worldwide"}
# Multi-country regions outside Europe, flagged only when no region that includes Europe is named:
# "Remote (EMEA or APAC)" and "Remote worldwide, APAC preferred" stay eligible.
NON_EU_REGION_TERMS = {
    "latam",
    "latin america",
    "america latina",
    "latinoamerica",
    "apac",
    "asia pacific",
    "asia-pacific",
}
NON_EU_REGION_OVERRIDE_TERMS = NON_EU_SCOPED_COUNTRY_OVERRIDE_TERMS | {"worldwide"}

EU_COUNTRY_TERMS = {
    "austria",
    "belgium",
    "bulgaria",
    "croatia",
    "cyprus",
    "czech republic",
    "czechia",
    "denmark",
    "estonia",
    "finland",
    "france",
    "germany",
    "greece",
    "hungary",
    "ireland",
    "italy",
    "latvia",
    "lithuania",
    "luxembourg",
    "malta",
    "netherlands",
    "poland",
    "portugal",
    "romania",
    "slovakia",
    "slovenia",
    "spain",
    "espana",
    "sweden",
}
EU_SCOPE_TERMS = {"eea", "european economic area", "schengen", "european union"}
EU_TIMEZONE_TERMS = {"cet", "cest", "gmt+1", "gmt+2", "utc+1", "utc+2"}
EU_DIRECT_REMOTE_TERMS = {
    "remote eu",
    "remote - eu",
    "eu - remote",
    "within eu",
    "remote europe",
    "remote - europe",
    "europe - remote",
    "within europe",
    "remoto europa",
    "remoto en europa",
    "toda europa",
    "en toda europa",
    "remoto ue",
    "desde europa",
    "desde espana",
}
EMEA_DIRECT_TERMS = {
    "remote emea",
    "remote - emea",
    "emea - remote",
    "within emea",
    "across emea",
    "emea candidates",
    "candidates in emea",
    "emea residents",
    "based in emea",
    "hiring in emea",
    "location: emea",
}
US_STATE_TERMS_WB = {
    "california",
    "new york",
    "texas",
    "washington",
    "colorado",
    "massachusetts",
    "illinois",
    "georgia",
    "virginia",
    "florida",
    "north carolina",
    "oregon",
    "pennsylvania",
    "maryland",
    "ohio",
    "arizona",
    "michigan",
    "minnesota",
    "utah",
    "connecticut",
}
US_CITY_TERMS = {
    "san francisco",
    "new york city",
    "nyc",
    "seattle",
    "austin",
    "boston",
    "chicago",
    "los angeles",
    "denver",
    "atlanta",
    "washington dc",
    "washington, dc",
    "washington d.c.",
    "washington, d.c.",
    "silicon valley",
    "mountain view",
    "palo alto",
    "sunnyvale",
    "menlo park",
    "san jose",
    "san mateo",
    "cupertino",
    "redmond",
}
US_EXPLICIT_TERMS = {
    "united states",
    "united states of america",
    "usa remote",
    "us remote",
    "u.s. (remote)",
    "u.s. remote",
    "location: u.s.",
    "location: u.s.a",
    "location: usa",
    "us-based",
    "us based",
    "u.s. based",
    "u.s.-based",
    "location: united states",
    "estados unidos",
    "ee.uu.",
    "ee.uu",
    "ee. uu.",
    "eeuu",
}
US_REMOTE_LOCATION_PATTERNS = (
    r"\bremote\s*[-–]?\s*\(?\s*u\.?\s*s\.?\s*a?\s*\)?\b",
    r"\blocation\s*:\s*united states\b",
    r"\bunited states of america\b",
)

# A security-clearance requirement only reads as a US regional exclusion with one of these
# cues. The bare pronoun "us" ("work with us") is deliberately absent.
US_CLEARANCE_REGION_TERMS = {
    "usa",
    "u.s.",
    "united states",
    "federal",
    "dod",
    "us citizen",
    "us citizens",
    "us person",
    "us persons",
    "us national",
    "us nationals",
}

SPAIN_NEGATION_TERMS = (
    "cannot hire",
    "not able to hire",
    "unable to hire",
    "except",
    "excluding",
    "outside",
)

# Inline literals of MARK III's ``classify_remote_policy`` (same order, same values).
REMOTE_EUROPE_PHRASES = (
    "remote europe",
    "within europe",
    "remoto europa",
    "remoto en europa",
    "toda europa",
    "en toda europa",
    "desde europa",
)
REMOTE_EU_PHRASES = ("remote eu", "within eu", "remoto ue")
HYPHENATED_REMOTE_EUROPE_PHRASES = ("remote - europe", "europe - remote")
HYPHENATED_REMOTE_EU_PHRASES = ("remote - eu", "eu - remote")
SPAIN_TERMS = ("spain", "espana")
SPAIN_LOCALITY_TERMS = (
    "galicia",
    "a coruna",
    "coruna",
    "vigo",
    "santiago de compostela",
    "pontevedra",
    "lugo",
    "ourense",
    "madrid",
    "barcelona",
    "valencia",
    "sevilla",
    "seville",
    "bilbao",
    "malaga",
    "zaragoza",
    "pamplona",
    "san sebastian",
    "donostia",
    "valladolid",
    "oviedo",
    "gijon",
    "alicante",
    "murcia",
)

# Literals that other modules used inline (discovery ranking and ATS extraction).
# Behaviour is unchanged; they only live here so no other core file names European places.
EUROPEAN_RANK_HINTS = ("eu", "europe", "spain")
EXTRACTOR_LOCATION_MARKERS = ("spain", "europe", "eu", "emea")
EXTRACTOR_COMPENSATION_OVERRIDE_TERMS = ("eu", "europe", "emea", "spain", "espana")

# --- rules (was rules/hard_filters.py) -------------------------------------------------------


@lru_cache(maxsize=128)
def _fold_accents(text: str) -> str:
    """Strip combining marks so accented geo terms match ASCII vocabulary.

    E.g. "España" folds to "Espana" and can match the term "espana".
    """
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _has_positive_spain_signal(text: str) -> bool:
    """True when Spain is mentioned as a positive hiring signal.

    Accent-folds the text so "España" matches, and ignores mentions preceded
    by negation ("cannot hire in Spain", "excluding Spain", ...).
    """
    folded = _fold_accents(text).lower()
    for term in SPAIN_TERMS:
        for match in word_boundary_pattern(term).finditer(folded):
            prefix = folded[max(0, match.start() - 30) : match.start()]
            if any(neg in prefix for neg in SPAIN_NEGATION_TERMS):
                continue
            return True
    return False


def _has_us_only_signal(text: str) -> bool:
    """Word-boundary "us only"/"usa only"/"u.s. only" with an idiom guard.

    Skips pronoun idioms like "join us only", "to us only", "for us only".
    """
    low = text.lower()
    for match in US_ONLY_PATTERN.finditer(low):
        prefix_words = low[max(0, match.start() - 12) : match.start()].split()
        if prefix_words and prefix_words[-1] in US_ONLY_IDIOM_PRECEDERS:
            continue
        return True
    return False


def _has_security_clearance_requirement(text: str) -> bool:
    """True for "security clearance" unless locally negated.

    "No security clearance required" / "does not require a security
    clearance" / "security clearance not required" must not fire.
    """
    low = text.lower()
    for match in SECURITY_CLEARANCE_PATTERN.finditer(low):
        prefix = low[max(0, match.start() - 30) : match.start()]
        suffix = low[match.end() : match.end() + 30]
        if any(neg in prefix for neg in SECURITY_CLEARANCE_NEGATIONS):
            continue
        if "not require" in suffix:
            continue
        return True
    return False


def _has_est_timezone_signal(text: str) -> bool:
    """True only when "est" appears with timezone context, not "Est. 2012"."""
    return EST_TZ_CONTEXT_PATTERN.search(text) is not None


def _has_disallowed_region_terms(text: str) -> bool:
    low = text.lower()
    if contains_any_word_boundary(low, DISALLOWED_REGION_TERMS_WB_PHRASES):
        return True
    if _has_us_only_signal(low):
        return True
    if not _has_security_clearance_requirement(low):
        return False
    # Clearance is not inherently US-specific. Require geographic/federal
    # context before treating it as a regional exclusion signal.
    return contains_any_word_boundary(low, US_CLEARANCE_REGION_TERMS)


def _iter_term_windows(text: str, term: str, *, radius: int = 80, word_boundary: bool = False) -> Iterable[str]:
    pattern = word_boundary_pattern(term) if word_boundary else re.compile(re.escape(term), re.IGNORECASE)
    for match in pattern.finditer(text):
        start = max(0, match.start() - radius)
        end = min(len(text), match.end() + radius)
        yield text[start:end]


def _has_pattern_match(text: str, patterns: Iterable[str]) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


def _window_has_us_disqualifier(window: str) -> bool:
    if contains_any_term_boundary(window, US_EXPLICIT_TERMS):
        return True
    if contains_any_substring(window, US_CITY_TERMS):
        return True
    if contains_any_word_boundary(window, US_STATE_TERMS_WB):
        return True
    return _has_pattern_match(window, US_REMOTE_LOCATION_PATTERNS)


def _has_eu_country_in_location_context(text: str) -> bool:
    return _has_term_in_location_context(text, EU_COUNTRY_TERMS, radius=80, word_boundary=True)


def _has_non_eu_location_in_context(text: str) -> bool:
    return _has_term_in_location_context(
        text,
        NON_EU_LOCATION_TERMS,
        radius=80,
        word_boundary=True,
        reject_if_us_disqualifier=False,
    )


def _has_scoped_non_eu_place_in_context(text: str) -> bool:
    """Non-EU countries/regions that only count when the text names no European (or worldwide) scope."""
    for terms, overrides in (
        (NON_EU_SCOPED_COUNTRY_TERMS, NON_EU_SCOPED_COUNTRY_OVERRIDE_TERMS),
        (NON_EU_REGION_TERMS, NON_EU_REGION_OVERRIDE_TERMS),
    ):
        if contains_any_word_boundary(text, overrides):
            continue
        if _has_term_in_location_context(text, terms, radius=80, word_boundary=True, reject_if_us_disqualifier=False):
            return True
    return False


def _has_term_in_location_context(
    text: str,
    terms: Iterable[str],
    *,
    radius: int = 80,
    word_boundary: bool = False,
    require_location_context: bool = True,
    reject_if_us_disqualifier: bool = True,
) -> bool:
    for term in terms:
        for window in _iter_term_windows(text, term, radius=radius, word_boundary=word_boundary):
            if require_location_context and not contains_any_substring(window, LOCATION_CONTEXT_TERMS):
                continue
            if reject_if_us_disqualifier and _window_has_us_disqualifier(window):
                continue
            if contains_any_substring(window, EXPLICIT_HIRING_SCOPE_TERMS):
                return True
            cleaned_window = re.sub(r"\bhome\s+offices?\s+in\b", "home_workplace", window, flags=re.IGNORECASE)
            if contains_any_substring(cleaned_window, INCIDENTAL_GEO_TERMS):
                continue
            return True
    return False


def _has_us_state_in_location_context(text: str) -> bool:
    for state in US_STATE_TERMS_WB:
        for window in _iter_term_windows(text, state, radius=80, word_boundary=True):
            if not contains_any_substring(window, LOCATION_CONTEXT_TERMS):
                continue
            # Avoid false positives like Georgia country when explicit EU context is present.
            if contains_any_word_boundary(window, EU_SCOPE_TERMS):
                continue
            if contains_any_word_boundary(window, {"europe", "eu", "eea", "schengen"}) or contains_any_substring(
                window, EU_COUNTRY_TERMS
            ):
                continue
            cleaned_window = re.sub(r"\bhome\s+offices?\s+in\b", "home_workplace", window, flags=re.IGNORECASE)
            if contains_any_substring(cleaned_window, INCIDENTAL_GEO_TERMS) and not contains_any_substring(
                window, EXPLICIT_HIRING_SCOPE_TERMS
            ):
                continue
            if state == "georgia" and not signals.georgia_window_is_us_state(window):
                continue
            return True
    return False


def _has_eu_eligibility_signal(text: str) -> bool:
    if _has_positive_spain_signal(text):
        return True
    if contains_any_substring(text, EU_DIRECT_REMOTE_TERMS):
        return True
    if _has_term_in_location_context(text, EU_SCOPE_TERMS, word_boundary=True):
        return True
    if _has_eu_country_in_location_context(text):
        return True
    if contains_any_substring(text, EMEA_DIRECT_TERMS):
        return True
    return bool(contains_any_word_boundary(text, EU_TIMEZONE_TERMS) and contains_any_substring(text, REMOTE_HINT_TERMS))


def _has_us_city_in_context(text: str) -> bool:
    for city in US_CITY_TERMS:
        for window in _iter_term_windows(text, city, radius=80, word_boundary=False):
            if contains_any_substring(window, EXPLICIT_HIRING_SCOPE_TERMS):
                return True
            cleaned_window = re.sub(r"\bhome\s+offices?\s+in\b", "home_workplace", window, flags=re.IGNORECASE)
            if contains_any_substring(cleaned_window, INCIDENTAL_GEO_TERMS) and not re.search(
                r"\b(?:location\s*:|based in|hiring in|candidates in)\b", cleaned_window, re.IGNORECASE
            ):
                continue
            return True
    return False


_US_STATE_CODE_RE = re.compile(
    r",\s*(?:AL|AK|AZ|AR|CA|CO|CT|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|MI|MN|MO|MS|MT|"
    r"NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VA|VT|WA|WI|WV|WY|DC)\b"
)


def _has_us_state_code_signal(raw_text: str) -> bool:
    for match in _US_STATE_CODE_RE.finditer(raw_text or ""):
        window = (raw_text or "")[max(0, match.start() - 60) : min(len(raw_text or ""), match.end() + 40)]
        low_w = window.lower()
        if any(term in low_w for term in INCIDENTAL_GEO_TERMS) or contains_any_substring(low_w, EU_COUNTRY_TERMS):
            continue
        return True
    return False


def _has_us_location_signal(text: str, *, raw_text: str = "") -> bool:
    if contains_any_term_boundary(text, US_EXPLICIT_TERMS):
        return True
    if _has_us_city_in_context(text):
        return True
    if _has_pattern_match(text, US_REMOTE_LOCATION_PATTERNS):
        return True
    if raw_text and _has_us_state_code_signal(raw_text):
        return True
    return _has_us_state_in_location_context(text)


def has_region_red_flags(text: str) -> bool:
    raw = text or ""
    low = normalize_text(raw)
    has_strong_us = _has_disallowed_region_terms(low)
    has_tz_us = contains_any_word_boundary(low, DISALLOWED_REGION_TERMS_WB) or _has_est_timezone_signal(low)
    has_loc_us = _has_us_location_signal(low, raw_text=raw)
    has_loc_non_eu = _has_non_eu_location_in_context(low) or _has_scoped_non_eu_place_in_context(low)
    has_eu_signal = _has_eu_eligibility_signal(low)

    if has_strong_us and not has_eu_signal:
        return True
    if has_loc_non_eu and not has_eu_signal:
        return True
    return bool((has_tz_us or has_loc_us) and not has_eu_signal)


def classify_eu_remote_policy(text: str) -> RemotePolicy | None:
    """Classify explicit European remote geography without inventing missing evidence.

    ``None`` means that the text did not provide a reliable policy signal.
    """
    low = normalize_text(text)
    if _has_positive_spain_signal(low):
        return "remote_spain"
    if any(token in low for token in REMOTE_EUROPE_PHRASES):
        return "remote_europe"
    if any(token in low for token in REMOTE_EU_PHRASES):
        return "remote_eu"
    if _has_term_in_location_context(low, EU_SCOPE_TERMS, word_boundary=True):
        return "remote_eu"
    if _has_eu_country_in_location_context(low):
        return "remote_eu"
    if contains_any_substring(low, EMEA_DIRECT_TERMS):
        return "remote_emea"
    if contains_any_word_boundary(low, EU_TIMEZONE_TERMS) and contains_any_substring(low, REMOTE_HINT_TERMS):
        return "remote_europe"
    if any(token in low for token in HYPHENATED_REMOTE_EUROPE_PHRASES):
        return "remote_europe"
    if any(token in low for token in HYPHENATED_REMOTE_EU_PHRASES):
        return "remote_eu"
    return None


def classify_eu_eligibility(text: str) -> Eligibility | None:
    """Classify EU/Spain eligibility from normalized textual evidence.

    The result is a confidence label used by the evaluator, not an automatic
    acceptance decision.
    """
    low = normalize_text(text)
    if _has_positive_spain_signal(low):
        return "confirmed"
    if contains_any_substring(low, EU_DIRECT_REMOTE_TERMS):
        return "high"
    if _has_term_in_location_context(low, EU_SCOPE_TERMS, word_boundary=True):
        return "high"
    if _has_eu_country_in_location_context(low):
        return "high"
    if contains_any_substring(low, EMEA_DIRECT_TERMS) and (
        contains_any_word_boundary(low, {"cet", "cest"}) or "european hours" in low or "eu entity" in low
    ):
        return "medium"
    if contains_any_word_boundary(low, EU_TIMEZONE_TERMS) and contains_any_substring(low, REMOTE_HINT_TERMS):
        return "medium"
    return None
