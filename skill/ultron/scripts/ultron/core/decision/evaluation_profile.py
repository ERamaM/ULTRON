"""Validated view of a persisted search-profile snapshot used by the heuristic evaluator.

Extracted from MARK III ``agent/prompt_builder.py`` (controller ruling P2-R2): only
``EvaluationProfile`` and the helpers it needs. ULTRON has no in-process prompt
builder; the agent reviews ambiguous jobs through the skill instructions instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ultron.core.remote_requirement import remote_required_value


def _profile_values(value: object) -> tuple[str, ...]:
    """Normalize profile lists without inventing values for missing fields."""
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        return ()
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return tuple(result)


def _profile_criteria(snapshot: dict[str, Any]) -> dict[str, Any]:
    criteria = snapshot.get("criteria")
    return dict(criteria) if isinstance(criteria, dict) else dict(snapshot)


def _role_family_label(role_terms: tuple[str, ...], target_titles: tuple[str, ...]) -> str:
    terms = role_terms or target_titles
    first = terms[0] if terms else "configured"
    first = re.sub(r"\b(senior|sr\.?|staff|principal|lead|junior|jr\.?)\b", "", first, flags=re.IGNORECASE)
    first = re.sub(r"\b(engineer|developer|scientist)\b", "", first, flags=re.IGNORECASE)
    first = " ".join(first.split()).strip(" -") or "configured"
    return f"{first.title()} engineering"


def _seniority_from_criteria(criteria: dict[str, Any], target_titles: tuple[str, ...]) -> str:
    seniority = criteria.get("seniority")
    if isinstance(seniority, dict):
        if seniority.get("required") is False:
            return "any"
        preferred = _profile_values(seniority.get("preferred_terms"))
        if preferred:
            return ", ".join(preferred)
        if seniority.get("required") is True:
            return "senior"
    corpus = " ".join(target_titles).lower()
    if re.search(r"\b(senior|sr\.?|staff|principal|lead)\b", corpus):
        return "senior"
    return "any"


def _target_regions(criteria: dict[str, Any]) -> tuple[str, ...]:
    location = criteria.get("location_scope")
    if isinstance(location, dict):
        return _profile_values(location.get("preferred"))
    return _profile_values(location)


@dataclass(frozen=True, slots=True)
class EvaluationProfile:
    """Validated, prompt-facing view of a persisted search-profile snapshot."""

    role_family_label: str
    role_terms: tuple[str, ...]
    seniority: str
    remote_policy: str
    target_regions: tuple[str, ...]
    eligibility_question: str
    exclusions: tuple[str, ...]
    specialties: tuple[str, ...]
    role_signal_terms: tuple[str, ...]
    data_scientist_signal_terms: tuple[str, ...]
    seniority_required: bool = False
    seniority_preferred_terms: tuple[str, ...] = ()
    seniority_excluded_terms: tuple[str, ...] = ()
    examples: tuple[dict[str, Any], ...] | None = None
    slug: str = ""

    @classmethod
    def from_search_profile(cls, snapshot: dict[str, Any]) -> EvaluationProfile:
        if not isinstance(snapshot, dict):
            raise TypeError("search profile snapshot must be an object")
        criteria = _profile_criteria(snapshot)
        role_terms = _profile_values(criteria.get("role_focus_terms"))
        target_titles = _profile_values(criteria.get("target_titles"))
        role_terms = role_terms or target_titles
        if not role_terms:
            raise ValueError("search profile must define role_focus_terms or target_titles")
        regions = _target_regions(criteria)
        region_label = ", ".join(region.removeprefix("Remote ") for region in regions)
        question_target = region_label or "the role's stated location"
        examples_raw = snapshot.get("worked_examples", snapshot.get("examples"))
        examples: tuple[dict[str, Any], ...] | None = None
        if isinstance(examples_raw, list):
            normalized = tuple(dict(item) for item in examples_raw if isinstance(item, dict))
            examples = normalized or None
        query_templates = snapshot.get("query_templates")
        specialties = _profile_values(criteria.get("specialties"))
        if not specialties and isinstance(query_templates, dict):
            specialties = _profile_values(query_templates.get("specialties"))
        remote_policy = remote_required_value(criteria)
        seniority_raw = criteria.get("seniority")
        seniority_raw = seniority_raw if isinstance(seniority_raw, dict) else {}
        return cls(
            role_family_label=str(criteria.get("role_family_label") or snapshot.get("role_family_label") or "").strip()
            or _role_family_label(role_terms, target_titles),
            role_terms=role_terms,
            seniority=_seniority_from_criteria(criteria, target_titles),
            seniority_required=bool(seniority_raw.get("required", False)),
            seniority_preferred_terms=_profile_values(seniority_raw.get("preferred_terms")),
            seniority_excluded_terms=_profile_values(seniority_raw.get("excluded_terms")),
            remote_policy=remote_policy,
            target_regions=regions,
            eligibility_question=f"Can someone based in {question_target} apply for this role?",
            exclusions=_profile_values(criteria.get("exclusion_terms")),
            specialties=specialties,
            role_signal_terms=_profile_values(criteria.get("role_signal_terms")) or specialties,
            data_scientist_signal_terms=_profile_values(criteria.get("data_scientist_signal_terms")),
            examples=examples,
            slug=str(snapshot.get("slug") or "").strip(),
        )
