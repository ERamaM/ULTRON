"""Immutable profile vocabulary shared by ATS and seed discovery.

Family defaults (fallback titles/signals and the family-aware ``ai_profile`` switch)
come from the active ``VocabularyPack``; with an empty pack only profile terms count.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ultron.core.rules.role_matching import filter_negatives_against_focus
from ultron.core.rules.term_matching import find_terms
from ultron.core.rules.title_synonyms import expand_role_terms
from ultron.core.vocabulary import VocabularyPack

COARSE_TECH_ROLE_TERMS = ("engineer", "scientist", "researcher", "developer", "architect", "platform")
EXCLUDED_TITLE_TERMS_WB = ("manager", "director", "vp", "head")
EXCLUDED_TITLE_TERMS_SUBSTR = ("team lead", "tech lead")
JUNIOR_TITLE_TERMS = ("intern", "junior", "graduate", "entry")
SENIOR_TITLE_TERMS = ("senior", "sr.", "sr ", " iii", " iv")


@dataclass(frozen=True, slots=True)
class TitleVocabulary:
    role_terms: tuple[str, ...]
    signal_terms: tuple[str, ...]
    excluded_terms: tuple[str, ...]
    ai_profile: bool
    preferred_levels: tuple[str, ...]


def _terms(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict.fromkeys(str(term).strip().lower() for term in value if isinstance(term, str) and term.strip()))


def build_title_vocabulary(
    criteria: Mapping[str, object] | None = None,
    *,
    vocabulary: VocabularyPack,
    role_focus_terms: Sequence[str] = (),
) -> TitleVocabulary:
    criteria = criteria or {}
    focus_terms = _terms(criteria.get("role_focus_terms"))
    target_titles = _terms(criteria.get("target_titles"))
    if not vocabulary.role_terms:
        roles = tuple(dict.fromkeys((*focus_terms, *target_titles))) or _terms(list(role_focus_terms))
    else:
        roles = focus_terms or target_titles or _terms(list(role_focus_terms))
    signals = _terms(criteria.get("specialties")) + _terms(criteria.get("role_signal_terms"))
    seniority = criteria.get("seniority")
    seniority = seniority if isinstance(seniority, dict) else {}
    seniority_required = bool(seniority.get("required", True))
    preferred = _terms(seniority.get("preferred_terms"))
    defaults: tuple[str, ...] = (
        *EXCLUDED_TITLE_TERMS_WB,
        *EXCLUDED_TITLE_TERMS_SUBSTR,
    )
    if seniority_required:
        defaults = (*defaults, *JUNIOR_TITLE_TERMS, "staff", "principal", "lead")
    negatives = (*defaults, *_terms(criteria.get("exclusion_terms")), *_terms(seniority.get("excluded_terms")))
    words = set(re.findall(r"\w+", " ".join((*roles, *target_titles, *preferred))))
    excluded = tuple(sorted(filter_negatives_against_focus(negatives, words)))
    return TitleVocabulary(
        tuple(expand_role_terms(roles, extra_groups=vocabulary.title_synonym_groups)) if roles else vocabulary.title_terms,
        tuple(dict.fromkeys(signals)) if roles else vocabulary.extra_title_terms,
        excluded,
        bool(vocabulary.role_terms)
        and (not roles or any(find_terms(term, vocabulary.role_terms) for term in (*roles, *signals))),
        preferred if roles else ("senior", "sr."),
    )
