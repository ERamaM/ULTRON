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
    classify_eu_eligibility,
    classify_eu_remote_policy,
    has_region_red_flags,
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
