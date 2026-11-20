"""One source-grounded geography assessment shared by every pipeline gate.

European rules (``geo_eu``) only apply to profiles whose scope is European; ``geo_scope``
derives that scope once per profile and callers pass it down explicitly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ultron.core.models import Eligibility, GeoScope, RemotePolicy
from ultron.core.rules import hard_filters_data as rules_data
from ultron.core.rules.geo_eu import (
    EEA_NON_EU_COUNTRY_TERMS,
    EU_COUNTRY_TERMS,
    EU_SCOPE_TERMS,
    EUROPEAN_SCOPE_TOKENS,
    NON_EU_SCOPED_COUNTRY_TERMS,
    classify_eu_eligibility,
    classify_eu_remote_policy,
    has_region_red_flags,
    home_locality_terms,
)
from ultron.core.rules.term_matching import find_terms, normalize_text
from ultron.core.rules.text_patterns import contains_any_substring

__all__ = [
    "EUROPEAN_SCOPE_TOKENS",
    "GeoAssessment",
    "assess_geography",
    "build_geo_body_excerpt",
    "geo_scope",
    "is_european_scope",
    "location_scope",
    "scoped_eligibility_terms",
]


def build_geo_body_excerpt(body_text: str, *, max_chars: int = 1200) -> str:
    text = (body_text or "").strip()
    if not text:
        return ""
    normalized_max = max(250, int(max_chars))
    selected_lines: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw_line in re.split(r"[\r\n]+", text or ""):
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        low = line.lower()
        if not contains_any_substring(low, rules_data.GEO_BODY_CONTEXT_TERMS):
            continue
        key = low
        if key in seen:
            continue
        seen.add(key)
        chunk = line[:400]
        extra = len(chunk) + (1 if selected_lines else 0)
        if selected_lines and total + extra > normalized_max:
            break
        selected_lines.append(chunk)
        total += extra
        if total >= normalized_max:
            break
    return "\n".join(selected_lines)


# Any EU or EEA member country (multi-word names such as "Czech Republic" included) names a European scope.
_EUROPEAN_COUNTRY_TERMS = tuple(sorted(EU_COUNTRY_TERMS | EEA_NON_EU_COUNTRY_TERMS | EU_SCOPE_TERMS))


def is_european_scope(target_regions: Iterable[str]) -> bool:
    regions = [str(region or "") for region in target_regions]
    return any(
        token in EUROPEAN_SCOPE_TOKENS
        for region in regions
        for token in re.findall(r"\w+", normalize_text(region))
    ) or any(find_terms(region, _EUROPEAN_COUNTRY_TERMS) for region in regions)


def location_scope(profile: Mapping[str, Any] | None) -> dict[str, tuple[str, ...]]:
    """``criteria.location_scope`` as ``preferred``/``eligibility_terms``/``excluded_terms`` tuples."""
    criteria = (profile or {}).get("criteria")
    raw = criteria.get("location_scope") if isinstance(criteria, Mapping) else None
    raw = raw if isinstance(raw, Mapping) else {}

    def _values(key: str) -> tuple[str, ...]:
        value = raw.get(key)
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(str(item).strip() for item in value if str(item or "").strip())

    return {key: _values(key) for key in ("preferred", "eligibility_terms", "excluded_terms")}


GENERIC_REGION_TOKENS = frozenset({"remote", "worldwide", "anywhere", "global"})
_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])(?<!\b[A-Z]\.)(?<!\b(?:[Ss]t|[Mm]t|[Ff]t)\.) +(?=[A-Z])")


def scoped_eligibility_terms(profile: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Non-European eligibility terms: ``eligibility_terms`` plus the ``preferred`` regions.

    Preferred values are normalized and stripped of the generic tokens in
    ``GENERIC_REGION_TOKENS`` ("Remote LATAM" -> "latam"); a value left empty is dropped.
    """
    scope = location_scope(profile)
    terms = [normalize_text(term) for term in scope["eligibility_terms"]]
    for region in scope["preferred"]:
        words = [word for word in re.findall(r"\w+", normalize_text(region)) if word not in GENERIC_REGION_TOKENS]
        if words:
            terms.append(" ".join(words))
    return tuple(dict.fromkeys(term for term in terms if term))


def scoped_posting_eligibility(location: str, remote_text: str, body_text: str, terms: Iterable[str]) -> Eligibility | None:
    """Use structured location or geographic context for non-European profiles."""
    terms = tuple(terms)
    prose_terms = tuple(term for term in terms if term != "us")
    if find_terms(location, terms) or find_terms(remote_text, prose_terms):
        return "high"
    # Limit the context to the sentence that carries the geography signal.
    body_excerpt = build_geo_body_excerpt(_SENTENCE_BOUNDARY.sub("\n", body_text))
    # In prose, the lowercase pronoun "us" is not a country abbreviation.
    if "us" in terms and re.search(r"(?<!\w)US(?!\w)", f"{remote_text}\n{body_excerpt}"):
        return "high"
    return "high" if find_terms(body_excerpt, prose_terms) else None


# Regions that span several countries. A posting naming one is open beyond a single country, and a profile's
# preferred value naming one is a region, not a home country.
WIDE_REGION_WORDS = frozenset({
    "europe", "european", "europa", "eu", "ue", "eea", "emea", "schengen", "latam", "apac", "americas",
    "worldwide", "anywhere", "global", "international", "nordics", "dach", "benelux", "mundo",
})
WIDE_REGION_PHRASES = (
    "latin america", "america latina", "latinoamerica", "north america", "south america", "asia pacific",
    "any country", "european union", "european economic area", "cualquier pais",
)
# Countries a posting may restrict work to. EU and EEA members come from geo_eu; the rest are the other
# countries that commonly appear in a posting's location field.
COUNTRY_TERMS = tuple(sorted({
    *EU_COUNTRY_TERMS, *EEA_NON_EU_COUNTRY_TERMS, *NON_EU_SCOPED_COUNTRY_TERMS,
    "united kingdom", "uk", "switzerland", "united states", "usa", "canada", "brazil", "brasil", "mexico",
    "india", "australia", "new zealand", "japan", "singapore", "philippines", "ukraine", "serbia", "costa rica",
}))


# Spellings of one country: a profile that says "Brazil" is at home in a posting that says "Brasil".
COUNTRY_ALIASES = (
    ("united kingdom", "uk", "great britain", "england", "scotland", "wales"),
    ("united states", "usa", "us", "united states of america"),
    ("brazil", "brasil"),
    ("netherlands", "holland"),
    ("germany", "deutschland"),
    ("czech republic", "czechia"),
    ("norway", "norge"),
    ("turkey", "turkiye", "turquia"),
)
_COUNTRY_NAMES = frozenset(COUNTRY_TERMS) | {name for group in COUNTRY_ALIASES for name in group}


def _country_spellings(place: str) -> tuple[str, ...]:
    return next((group for group in COUNTRY_ALIASES if place in group), (place,))


# "global", "international" and "worldwide" in a body name a region, as any wide-region word does, unless they
# directly modify a company noun: "the global economy", "our global support organization", "a global leader",
# "international clients", "Global Candidate Privacy Notice". The list
# is closed on purpose: anything else ("a global remote role", "international applicants may apply") still opens
# the posting, because ambiguous text must never demote a keep. A negated statement ("we are not hiring
# internationally") also still opens it: a known limit of this conservative reading. In a location field these
# words always name a region ("Remote, Global").
COMPANY_SCOPE_WORDS = frozenset({"global", "international"})
_SCOPE_WORD = re.compile(r"\b(?:global|international|worldwide)\b")
_WORD = re.compile(r"[a-z]+")
_COMPANY_NOUNS = frozenset({
    "economy", "leader", "leaders", "company", "companies", "brand", "brands", "business", "businesses",
    "organisation", "organisations", "organization", "organizations", "platform", "platforms", "team", "teams",
    "presence", "footprint", "network", "networks", "scale", "client", "clients", "customer", "customers",
    "market", "markets", "expansion", "community", "operations", "offices", "headquarters", "provider",
    "providers", "firm", "enterprise", "group", "distributed", "privacy", "notice", "policy", "reach", "impact",
    "partners", "portfolio", "supply", "chain", "player", "payments", "payouts",
})
# The scope word must modify the company noun directly: the noun is the next word ("global economy",
# "international clients") or follows one modifier inside the same noun phrase ("global support organization",
# "global payments platform"). A function or work-arrangement word in between ends the noun phrase, so
# "worldwide with our team" and "a global remote team" keep their scope.
_NOUN_PHRASE_BREAKERS = frozenset({
    "a", "an", "the", "our", "your", "their", "its", "this", "that", "we", "you", "and", "or", "but", "with",
    "to", "for", "in", "of", "on", "at", "by", "from", "as", "across", "within", "is", "are", "be", "remote",
    "remotely", "hybrid", "virtual", "flexible",
})
# Statements that open a posting without a wide-region word. "Globally" alone is an adverb ("monitor quality
# globally"), so only these hiring statements use it.
_OPEN_SCOPE_PHRASES = (
    "no country restrictions", "no location restrictions", "without location restrictions",
    "without country restrictions", "anywhere in the world", "hire globally", "hiring globally",
    "hire internationally", "hiring internationally", "globally remote", "candidates globally",
    "applicants globally",
)


def _modifies_company_noun(following: list[str]) -> bool:
    """True when the words after a scope word make it a company description (see above)."""
    if not following:
        return False
    if following[0] in _COMPANY_NOUNS:
        return True
    return len(following) > 1 and following[0] not in _NOUN_PHRASE_BREAKERS and following[1] in _COMPANY_NOUNS


def _body_names_region(text: str) -> bool:
    """True when a body names a multi-country scope, not counting company descriptions (see above)."""
    low = normalize_text(text)
    for match in _SCOPE_WORD.finditer(low):
        window_end = match.end() + 80
        clause_end = _CLAUSE_BREAK.search(low, match.end(), window_end)  # the window stops at its sentence
        following = _WORD.findall(low, match.end(), clause_end.start() if clause_end else window_end)[:2]
        if not _modifies_company_noun(following):
            return True
    if find_terms(low, _OPEN_SCOPE_PHRASES):
        return True
    return names_wide_region(_ANYWHERE_IN_PLACE.sub(" ", low), ignore=COMPANY_SCOPE_WORDS | {"worldwide"})


def names_wide_region(text: str, *, ignore: frozenset[str] = frozenset()) -> bool:
    """True when the text names a region that spans several countries ("Remote EU", "anywhere", "LATAM")."""
    low = normalize_text(text)
    return bool(find_terms(low, WIDE_REGION_WORDS - ignore) or find_terms(low, WIDE_REGION_PHRASES))


def profile_home_places(criteria: Mapping[str, Any] | None) -> tuple[str, ...]:
    """The single countries or cities in ``location_scope.preferred`` ("Ireland", "Dublin"), without regions.

    A profile that names only regions ("Remote EU", "Remote LATAM") has no home place, and the
    country-restriction check below does not apply to it.
    """
    places: list[str] = []
    for region in location_scope({"criteria": criteria or {}})["preferred"]:
        words = [word for word in re.findall(r"\w+", normalize_text(region)) if word not in GENERIC_REGION_TOKENS]
        if words and not names_wide_region(" ".join(words)):
            places.append(" ".join(words))
    return tuple(dict.fromkeys((*places, *home_locality_terms(places))))


_ANYWHERE_IN_PLACE = re.compile(r"\banywhere\s+in\s+(?:the\s+)?\w+")
# A standalone upper-case "US" names the United States ("US Remote", "US-SF"); lower-case "us" is a pronoun.
_US_CODE = re.compile(r"(?<![\w.])US(?![\w.])")
# Northern Ireland is in the United Kingdom, so it must not read as the home country of an Ireland profile.
_NORTHERN_IRELAND = re.compile(r"\bnorthern\s+ireland\b")


def _place_text(text: str) -> str:
    """Normalized text with "US" spelled out and "Northern Ireland" read as the United Kingdom."""
    return _NORTHERN_IRELAND.sub("united kingdom", normalize_text(_US_CODE.sub("United States", text or "")))


def _names_home(text: str, homes: Iterable[str]) -> bool:
    """True when the text names a home place, read both as written and through ``_place_text``.

    The rewritten text catches "US" for a United States profile; the text as written keeps a home place the
    profile names itself ("Northern Ireland", "Belfast"). Only a plain "Ireland" home is not matched in the
    text as written, because "Northern Ireland" contains it.
    """
    homes = tuple(homes)
    as_written = tuple(home for home in homes if home != "ireland")
    return bool(find_terms(_place_text(text), homes) or find_terms(normalize_text(text or ""), as_written))


# US places that name no country. State and territory codes after a comma, at the end of a location part
# ("Austin, TX", "Lewes, DE 19958"), are matched as written: "Co. Meath" and "CO. WESTMEATH" are Irish counties.
# Most codes are also a country code ("CA" Canada, "DE" Germany, "AR" Argentina) or a region code elsewhere
# ("PR" Parana, "DC" Bogota, "MI" Milan, "WA" Western Australia), so they only count before a ZIP code, and never
# when that country is a home place. The codes below have no such double reading and count on their own.
_US_STATE_CODES_ALONE = frozenset({
    "AK", "FL", "GU", "HI", "IA", "KS", "ND", "NH", "NJ", "NM", "NY", "OH", "OK", "TX", "WI", "WV", "WY",
})
_US_STATE_CODES = _US_STATE_CODES_ALONE | frozenset({
    "AK", "AL", "AR", "AZ", "CA", "CO", "CT", "DC", "DE", "FL", "GA", "GU", "HI", "IA", "ID", "IL", "IN", "KS",
    "KY", "LA", "MA", "MD", "ME", "MI", "MN", "MO", "MS", "MT", "NC", "ND", "NE", "NH", "NJ", "NM", "NV", "NY",
    "OH", "OK", "OR", "PA", "PR", "RI", "SC", "SD", "TN", "TX", "UT", "VA", "VT", "WA", "WI", "WV", "WY",
})
_ISO_COUNTRY_OF_STATE_CODE = {
    "AL": "albania", "AR": "argentina", "AZ": "azerbaijan", "CA": "canada", "CO": "colombia", "DE": "germany",
    "GA": "gabon", "ID": "indonesia", "IL": "israel", "IN": "india", "KY": "cayman islands", "LA": "laos",
    "MA": "morocco", "MD": "moldova", "ME": "montenegro", "MN": "mongolia", "MO": "macao", "MS": "montserrat",
    "MT": "malta", "NC": "new caledonia", "NE": "niger", "PA": "panama", "SC": "seychelles", "SD": "sudan",
    "TN": "tunisia", "VA": "vatican city",
}
# Codes that are also a region of another country: Italian provinces ("Milano, MI 20121" has a five-digit postal
# code too), Brazilian states, Indian states, Bogota D.C. and Western Australia. Such a code
# never counts when that country is a home place.
_REGION_CODE_COUNTRIES: dict[str, frozenset[str]] = {
    code: frozenset(countries) for code, countries in {
        "AL": ("italy", "brazil", "brasil"), "AR": ("italy", "india"), "CA": ("italy",),
        "CO": ("italy",), "CT": ("italy",), "GA": ("india",), "MA": ("brazil", "brasil"),
        "ME": ("italy",), "MI": ("italy",), "MN": ("italy", "india"), "MO": ("italy",),
        "MS": ("italy", "brazil", "brasil"), "MT": ("italy", "brazil", "brasil"), "OR": ("italy", "india"),
        "PA": ("italy", "brazil", "brasil"), "PR": ("italy", "brazil", "brasil"), "RI": ("italy",),
        "SC": ("brazil", "brasil"), "TN": ("italy", "india"), "UT": ("india",), "VA": ("italy",),
        "VT": ("italy",), "DC": ("colombia",), "WA": ("australia",),
    }.items()
}
# Federations whose own states license professions: a "state licence" there is not US evidence.
_STATE_LICENSING_COUNTRIES = frozenset({"australia", "india", "nigeria", "malaysia"})
_US_STATE_CODE_AT_END = re.compile(r",\s*([A-Z]{2})(\s+\d{5}(?:-\d{4})?)?\s*(?=$|[;|/\n])", re.MULTILINE)
# State names at the end of a location part ("Memphis, Tennessee") or before "(Any City)". Left out: "Georgia"
# (also a country), a bare "Washington" (also a town in England), "Florida" (also a department of Uruguay) and
# "Montana" (also a province of Bulgaria). "Virginia, Co. Cavan" is an Irish town: a state name only counts after a
# comma.
US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware",
    "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
    "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio", "oklahoma",
    "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas", "utah",
    "vermont", "virginia", "west virginia", "wisconsin", "wyoming", "washington state", "district of columbia",
)
_STATE_NAME = "|".join(re.escape(name) for name in sorted(US_STATE_NAMES, key=len, reverse=True))
_US_STATE_NAME_IN_PLACE = re.compile(
    rf",\s*(?:{_STATE_NAME})\s*(?=$|[;|/\n])|\b(?:{_STATE_NAME})\s*\(\s*any\s+city\s*\)", re.IGNORECASE | re.MULTILINE
)
# A licence issued by a US state counts only in an explicit requirement on the applicant:
# - "must hold an active Texas RN license", "required to have a valid state licensure";
# - "current appropriate state licensure", "active state license is required";
# - "licensed to practice in the state of Arizona", "eligible for licensure in New York".
# Anything else ("software to manage state licenses for our current customers", "maintain our state license
# database") never counts, and a negation in the same clause ("current state licensure is not required") blocks
# it. Each pattern is a sequence of literal alternatives, so matching stays linear.
_LICENCE_STATE = rf"(?:(?:{_STATE_NAME})\s)?"
# Every alternative needs state-specific evidence: a US state name or the word "state" ("must hold a valid
# nursing license" and "must have a license to drive" are not US evidence).
_LICENCE_STATE_EVIDENCE = rf"(?:(?:{_STATE_NAME})\s(?:state\s)?|state\s)"
_LICENCE_KIND = r"(?:(?:rn|nursing|professional)\s)?"
_LICENCE_NOUN = r"licen[cs](?:e|ure)\b"
_US_STATE_LICENCE_REQUIREMENT = re.compile(
    r"\b(?:must|required\sto)\s(?:hold|have|possess|maintain|obtain)\s(?:an?\s)?"
    rf"(?:(?:active|current|valid|unrestricted)\s)?{_LICENCE_STATE_EVIDENCE}{_LICENCE_KIND}{_LICENCE_NOUN}"
    rf"|\b(?:active|current|valid|unrestricted)\s(?:appropriate\s)?{_LICENCE_STATE}state\s{_LICENCE_KIND}"
    rf"{_LICENCE_NOUN}"
    rf"|\blicensed\s(?:to\spractice\s)?in\sthe\sstate\sof\s(?:{_STATE_NAME})\b"
    rf"|\beligible\sfor\s{_LICENCE_KIND}licensure\sin\s(?:the\sstate\sof\s)?(?:{_STATE_NAME})\b",
)
# Clause and sentence ends: single characters, no surrounding whitespace in the pattern, so splitting is linear.
_CLAUSE_BREAK = re.compile(r"[.;!?\n•·▪●]")
# Every body path reads at most this many characters, with runs of horizontal whitespace collapsed.
_MAX_BODY_CHARS = 30_000
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")


def _bounded_body(text: str) -> str:
    return _HORIZONTAL_SPACE.sub(" ", (text or "")[:_MAX_BODY_CHARS])


# A negation or a preference in the clause blocks a licence match: "not required", "optional", "a plus".
_NEGATION_WORDS = frozenset({"no", "not", "never", "without", "cannot", "unable", "none"})
_PREFERENCE_WORDS = frozenset({
    "optional", "preferred", "desirable", "desired", "plus", "advantage", "advantageous", "beneficial", "bonus",
    "ideally", "preferably", "nice",
})


def _requires_us_state_licence(body_text: str) -> bool:
    """True when the body requires the applicant to hold or be eligible for a US state licence (see above)."""
    for clause in _CLAUSE_BREAK.split(normalize_text(body_text).replace("’", "'")):
        words = clause.split()
        tokens = set(_WORD.findall(clause))
        if (_NEGATION_WORDS | _PREFERENCE_WORDS) & tokens or any(word.endswith("n't") for word in words):
            continue
        if _US_STATE_LICENCE_REQUIREMENT.search(" ".join(words)):
            return True
    return False


def _names_us_place(fields: str, body_text: str, homes: Iterable[str]) -> bool:
    """True when the location fields or a state-licence requirement place a posting in the United States."""
    homes = set(homes)
    for match in _US_STATE_CODE_AT_END.finditer(fields):
        code, zip_code = match.group(1), match.group(2)
        if code in _US_STATE_CODES_ALONE:
            return True
        other_uses = {_ISO_COUNTRY_OF_STATE_CODE.get(code), *_REGION_CODE_COUNTRIES.get(code, ())}
        if zip_code and code in _US_STATE_CODES and not other_uses & homes:
            return True
    if _US_STATE_NAME_IN_PLACE.search(fields):
        return True
    return not _STATE_LICENSING_COUNTRIES & homes and _requires_us_state_licence(body_text)


def other_country_restriction(
    location: str, remote_text: str, criteria: Mapping[str, Any] | None, body_text: str = "",
    *, remote_job: bool = True,
) -> str | None:
    """The country a posting is limited to when it is none of the profile's home places, else ``None``.

    The country comes from the structured location and remote fields ("Remote - Netherlands", "Lyon, France").
    A posting is not limited when those fields also name a home place ("Germany, Ireland or Portugal") or a
    multi-country region ("Remote EU, Germany preferred"), or when the body does
    ("work remotely from anywhere in Ireland", "hiring across the EU"): when in doubt, nothing is limited.
    "Anywhere in the Netherlands" still names one country, so it does not count as a region, and "global" or
    "international" in the body do not count inside a closed list of company descriptions ("our global
    platform", "international clients"); elsewhere they still name a region.
    The body is only read for a remote job (``remote_job``): an on-site or hybrid job is done at its location,
    whatever regions the company blurb names.
    A US place that names no country ("Austin, TX", "Nebraska (Any City)") or a US state licence in the body of
    any job ("current state licensure") limits the posting to the United States, unless that is a home.
    """
    places = profile_home_places(criteria)
    countries = [spelling for place in places if place in _COUNTRY_NAMES for spelling in _country_spellings(place)]
    if not countries:  # only cities ("Dublin") or regions: the home country is not known, so nothing is limited
        return None
    extra = [normalize_text(term) for term in location_scope({"criteria": criteria or {}})["eligibility_terms"]]
    # Eligibility terms are read through the alias table too: "us" or "usa" makes "united states" a home.
    extra_countries = [spelling for term in extra if term in _COUNTRY_NAMES for spelling in _country_spellings(term)]
    homes = (*places, *countries, *extra_countries)
    fields = f"{location}\n{remote_text}"
    text = _place_text(fields)
    if names_wide_region(text) or _names_home(fields, homes):
        return None
    body_text = _bounded_body(body_text)
    excerpt = _place_text(body_text) if remote_job else ""
    body_region = _body_names_region(excerpt)
    if (remote_job and _names_home(body_text, homes)) or body_region:
        return None
    named = find_terms(text, COUNTRY_TERMS)
    if named:
        return named[0]
    if "united states" not in homes and _names_us_place(fields, body_text, homes):
        return "united states"
    return None


def geo_scope(profile: Mapping[str, Any] | None) -> GeoScope:
    """``"global"`` with no preferred regions, ``"eu"`` when one is European, else ``"other"``."""
    preferred = location_scope(profile)["preferred"]
    if not preferred:
        return "global"
    return "eu" if is_european_scope(preferred) else "other"


@dataclass(frozen=True, slots=True)
class GeoAssessment:
    european_scope: bool
    red_flags: bool
    remote_policy: RemotePolicy | None
    eligibility: Eligibility | None
    conflict: bool


def assess_geography(
    *,
    location: str,
    remote_text: str,
    body_text: str,
    target_regions: Iterable[str],
    eligibility_hint: str | None = None,
) -> GeoAssessment:
    """European geography assessment (MARK III). Only European-scope callers use it."""
    geo_text = f"{location}\n{remote_text}\n{build_geo_body_excerpt(body_text)}"
    scope = is_european_scope(target_regions)
    red_flags = has_region_red_flags(geo_text) or bool(
        location and has_region_red_flags(f"Location: {location}")
    )
    remote = classify_eu_remote_policy(geo_text)
    eligibility = classify_eu_eligibility(geo_text)
    positive = remote is not None or eligibility is not None or eligibility_hint is not None
    return GeoAssessment(scope, red_flags, remote, eligibility, scope and red_flags and not positive)
