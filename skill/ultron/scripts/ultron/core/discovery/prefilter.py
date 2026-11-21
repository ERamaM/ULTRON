from __future__ import annotations

import re
from typing import Protocol

from ultron.core.discovery.title_vocabulary import (
    COARSE_TECH_ROLE_TERMS,
    SENIOR_TITLE_TERMS,
    TitleVocabulary,
)
from ultron.core.models import GeoScope
from ultron.core.rules.geo_eu import (
    EU_COUNTRY_TERMS,
    EUROPEAN_RANK_HINTS,
    SPAIN_LOCALITY_TERMS,
    _fold_accents,
    _has_us_state_code_signal,
    has_region_red_flags,
)
from ultron.core.rules.term_matching import find_terms
from ultron.core.rules.text_patterns import (
    contains_any_term_boundary,
    contains_any_word_boundary,
    word_boundary_pattern,
)
from ultron.core.rules.title_synonyms import normalize_role_title
from ultron.core.vocabulary import VocabularyPack

_HYBRID_ONSITE_HINTS = ("hybrid", "onsite", "on-site", "in-office", "in office")
_STRONG_REMOTE_HINTS = ("fully remote", "100% remote", "remote-first", "remote first", "remote friendly", "remote-friendly")
_EU_POSITIVE_HINTS = tuple(dict.fromkeys((*EUROPEAN_RANK_HINTS, *EU_COUNTRY_TERMS, *SPAIN_LOCALITY_TERMS, "emea", "eea")))
_GLOBAL_SCOPE_HINTS = ("worldwide", "global", "anywhere")
_NON_EU_TITLE_LOC_RE = re.compile(
    r"(?:,|\s[-–|/]|\()[ \t]{0,4}(?:usa|u\.s\.a?\.?|united states|india|canada|brazil|australia|mexico|japan|singapore|uk|united kingdom)\b",
    re.IGNORECASE,
)


def _has_non_eu_title_location(title: str) -> bool:
    raw = str(title or "").strip()
    if not raw:
        return False
    low = _fold_accents(raw).lower()
    if any(word_boundary_pattern(term).search(low) for term in (*_EU_POSITIVE_HINTS, *_GLOBAL_SCOPE_HINTS)):
        return False
    return bool(_has_us_state_code_signal(raw) or _NON_EU_TITLE_LOC_RE.search(raw))


class PostingLike(Protocol):
    """Structural stand-in for MARK III's ``AtsPosting`` (its client module is not ported here)."""

    title: str
    location: str
    remote_hint: str


def _preferred_match_terms(regions: tuple[str, ...], eligibility_terms: tuple[str, ...]) -> tuple[str, ...]:
    out: list[str] = []
    for raw in (*regions, *eligibility_terms):
        cleaned = str(raw or "").strip().lower()
        if cleaned.startswith("remote "):
            cleaned = cleaned.removeprefix("remote ").strip()
        if cleaned and cleaned not in {"remote", "worldwide", "anywhere", "global"}:
            out.append(cleaned)
    return tuple(dict.fromkeys(out))


def is_clearly_ineligible_at_discovery(
    posting: PostingLike,
    *,
    geo: GeoScope,
    remote_required: bool = True,
    excluded_location_terms: tuple[str, ...] = (),
) -> bool:
    """Skip postings whose structured ATS location/remote metadata is unambiguously ineligible."""
    scope_text = f"{posting.location} {posting.remote_hint}".strip()
    if not scope_text:
        return False
    low = _fold_accents(scope_text).lower()
    if (
        remote_required
        and contains_any_term_boundary(low, _HYBRID_ONSITE_HINTS)
        and not any(term in low for term in _STRONG_REMOTE_HINTS)
    ):
        return True
    if excluded_location_terms and find_terms(scope_text, excluded_location_terms):
        return True
    if geo == "eu":
        if (
            "remote" not in low
            and not any(term in low for term in _GLOBAL_SCOPE_HINTS)
            and has_region_red_flags(f"Location: {scope_text}")
        ):
            return True
        if (
            not any(word_boundary_pattern(term).search(low) for term in (*_EU_POSITIVE_HINTS, *_GLOBAL_SCOPE_HINTS))
            and _has_non_eu_title_location(posting.title)
        ):
            return True
    return False


def posting_rank_score(
    posting: PostingLike,
    *,
    title_vocabulary: TitleVocabulary,
    vocabulary: VocabularyPack,
    geo: GeoScope,
    remote_required: bool = True,
    preferred_regions: tuple[str, ...] = (),
    eligibility_terms: tuple[str, ...] = (),
    excluded_location_terms: tuple[str, ...] = (),
) -> int:
    """Discovery ranking; boosts remote/regional matches and penalizes mismatched scopes."""
    title = posting.title.lower()
    score = 6 if find_terms(title, title_vocabulary.preferred_levels) else 0
    if find_terms(normalize_role_title(title), title_vocabulary.role_terms):
        score += 6
    has_signal = (
        contains_coarse_signal(title, vocabulary)
        if title_vocabulary.ai_profile
        else bool(find_terms(title, title_vocabulary.signal_terms))
    )
    if has_signal:
        score += 4
    if find_terms(title, title_vocabulary.signal_terms):
        score += 2
    if find_terms(title, title_vocabulary.excluded_terms):
        score -= 8
    scope_text = _fold_accents(f"{posting.location} {posting.remote_hint}".strip()).lower()
    if remote_required:
        if "remote" in scope_text:
            score += 6
        elif contains_any_term_boundary(scope_text, _HYBRID_ONSITE_HINTS) and not any(
            term in scope_text for term in _STRONG_REMOTE_HINTS
        ):
            score -= 8
    if geo == "eu":
        if any(word_boundary_pattern(term).search(scope_text) for term in _EU_POSITIVE_HINTS):
            score += 6
        elif has_region_red_flags(f"Location: {scope_text}") and not any(
            term in scope_text for term in _GLOBAL_SCOPE_HINTS
        ):
            score -= 10
    pref_terms = _preferred_match_terms(preferred_regions, eligibility_terms)
    if pref_terms and find_terms(scope_text, pref_terms):
        score += 6
    if excluded_location_terms and find_terms(scope_text, excluded_location_terms):
        score -= 10
    return score


def contains_coarse_signal(text: str, vocabulary: VocabularyPack) -> bool:
    """Coarse family signal from the active pack (MARK III ``contains_coarse_ai_signal``)."""
    if not text:
        return False
    low = text.lower()
    if contains_any_word_boundary(low, vocabulary.coarse_signal_wb):
        return True
    return any(term in low for term in vocabulary.coarse_signal_substr)


def passes_coarse_prefilter(
    posting: PostingLike, *, title_vocabulary: TitleVocabulary, vocabulary: VocabularyPack
) -> bool:
    """Admit the profile vocabulary before considering generic role fallback."""
    title = (posting.title or "").lower()
    if not title or find_terms(title, title_vocabulary.excluded_terms):
        return False
    if find_terms(normalize_role_title(title), title_vocabulary.role_terms):
        return True
    context = f"{title} {posting.location} {posting.remote_hint}".strip()
    if not title_vocabulary.ai_profile:
        return bool(find_terms(context, title_vocabulary.signal_terms))
    if not contains_coarse_signal(context, vocabulary):
        return False
    return contains_any_word_boundary(title, COARSE_TECH_ROLE_TERMS) or any(term in title for term in SENIOR_TITLE_TERMS)
