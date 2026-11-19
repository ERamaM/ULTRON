"""Deterministic, explainable profile-match scoring (score-v3)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from ultron.core.models import ExtractedJob
from ultron.core.ranker.contradictions import (
    evidence_covers_source,
    explicitly_contradicts,
)
from ultron.core.ranker.profile_terms import ProfileTermHits, exclusion_lists_configured
from ultron.core.rules.hard_filters_data import DISALLOWED_REMOTE_TERMS, ONSITE_TERMS
from ultron.core.rules.term_matching import find_terms

DimensionState = Literal["matched", "missing", "unknown", "not_required", "penalized"]
MatchBand = Literal["strong", "good", "borderline", "ineligible"]
SCORE_VERSION = "score-v3"


class MatchDimension(BaseModel):
    state: DimensionState
    evidence: str = ""
    weight: int = 0


class MatchScore(BaseModel):
    value: int = Field(ge=0, le=100)
    band: MatchBand
    version: str = SCORE_VERSION
    dimensions: dict[str, MatchDimension]
    matched_requirements: list[str] = Field(default_factory=list)
    missing_requirements: list[str] = Field(default_factory=list)
    uncertain_requirements: list[str] = Field(default_factory=list)
    hard_incompatibilities: list[str] = Field(default_factory=list)


_WEIGHTS = {"role": 35, "seniority": 15, "remote": 15, "eligibility": 25, "open_status": 10, "exclusions": 20}
_SENIOR_PREFERENCES = {"senior", "sr", "sr.", "staff", "principal", "lead", "distinguished"}


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _dimension(
    *,
    name: str,
    value: bool | None,
    evidence: str,
    required: bool,
    weight: int,
    contradiction_allowed: bool,
    role_terms: tuple[str, ...] = (),
    source: ExtractedJob | None = None,
) -> MatchDimension:
    if not required:
        return MatchDimension(state="not_required", evidence=evidence, weight=0)
    if value is True:
        return MatchDimension(state="matched", evidence=evidence, weight=weight)
    if (
        value is False
        and contradiction_allowed
        and evidence_covers_source(name, evidence, source)
        and explicitly_contradicts(name, evidence, role_terms=role_terms)
    ):
        return MatchDimension(state="missing", evidence=evidence, weight=weight)
    return MatchDimension(state="unknown", evidence=evidence, weight=weight)


def compute_match_score(
    evaluation: Any,
    criteria: dict[str, Any] | None,
    *,
    source: ExtractedJob | None,
    term_hits: ProfileTermHits | None = None,
) -> MatchScore:
    """Score an evaluation without inventing evidence or compensating hard failures."""

    criteria = dict(criteria or {})
    seniority = criteria.get("seniority") if isinstance(criteria.get("seniority"), dict) else {}
    remote_policy = criteria.get("remote_policy") if isinstance(criteria.get("remote_policy"), dict) else {}
    location_scope = criteria.get("location_scope") if isinstance(criteria.get("location_scope"), dict) else {}
    seniority_required = bool(seniority.get("required", False))
    preferred = seniority.get("preferred_terms")
    senior_contradiction_allowed = (
        isinstance(preferred, (list, tuple))
        and bool(preferred)
        and all(isinstance(term, str) and term.strip().lower() in _SENIOR_PREFERENCES for term in preferred)
    )
    remote_required = str(remote_policy.get("required") or "any") == "full_remote"
    eligibility_required = bool(location_scope.get("preferred"))
    role_sources = (criteria.get("role_focus_terms"), criteria.get("target_titles"))
    role_terms = (
        tuple(term for source in role_sources if source for term in source)
        if all(source is None or isinstance(source, (list, tuple)) for source in role_sources)
        else ()
    )

    closed_confirmed = _field(evaluation, "discard_category") == "closed_or_invalid"
    contradiction_allowed = _field(evaluation, "verdict") != "unsure"
    dimensions = {
        "role": _dimension(
            name="role",
            value=_field(evaluation, "is_allowed_family"),
            evidence=str(_field(evaluation, "role_evidence", "") or ""),
            required=True,
            weight=_WEIGHTS["role"],
            contradiction_allowed=contradiction_allowed,
            role_terms=role_terms,
            source=source,
        ),
        "seniority": _dimension(
            name="seniority",
            value=_field(evaluation, "is_senior"),
            evidence=str(_field(evaluation, "seniority_evidence", "") or ""),
            required=seniority_required,
            weight=_WEIGHTS["seniority"],
            contradiction_allowed=contradiction_allowed and senior_contradiction_allowed,
            source=source,
        ),
        "remote": _dimension(
            name="remote",
            value=_field(evaluation, "is_full_remote"),
            evidence=str(_field(evaluation, "remote_evidence", "") or ""),
            required=remote_required,
            weight=_WEIGHTS["remote"],
            contradiction_allowed=contradiction_allowed,
            source=source,
        ),
        "eligibility": _dimension(
            name="eligibility",
            value=(True if _field(evaluation, "eligibility") in {"confirmed", "high"} else None),
            evidence=str(_field(evaluation, "eligibility_evidence", "") or ""),
            required=eligibility_required,
            weight=_WEIGHTS["eligibility"],
            contradiction_allowed=contradiction_allowed,
            source=source,
        ),
        "open_status": _dimension(
            name="open_status",
            value=(False if closed_confirmed else True if _field(evaluation, "open_status_ok") is True else None),
            evidence=str(_field(evaluation, "open_status_evidence", "") or ""),
            required=True,
            weight=_WEIGHTS["open_status"],
            contradiction_allowed=contradiction_allowed,
            source=source,
        ),
    }

    exclusions_required = exclusion_lists_configured(criteria)
    dimensions["exclusions"] = MatchDimension(
        state=(
            "not_required"
            if not exclusions_required
            else "unknown"
            if term_hits is None
            else "penalized"
            if term_hits.penalties
            else "matched"
        ),
        evidence="; ".join(term_hits.penalties) if term_hits else "",
        weight=_WEIGHTS["exclusions"] if exclusions_required else 0,
    )
    if term_hits is not None:
        onsite = source is None or bool(
            find_terms(
                "\n".join((source.remote_text, source.location, source.body_text)),
                ONSITE_TERMS | DISALLOWED_REMOTE_TERMS,
            )
        )
        if (
            dimensions["remote"].state == "unknown"
            and term_hits.remote_positive
            and not term_hits.remote_negative
            and not onsite
        ):
            dimensions["remote"].state = "matched"
            dimensions["remote"].evidence = "Remote keywords: " + ", ".join(term_hits.remote_positive)
        if term_hits.location_eligibility:
            dimensions["eligibility"].evidence += " | Location keywords: " + ", ".join(term_hits.location_eligibility)

    matched = [name for name, item in dimensions.items() if item.state == "matched"]
    missing = [name for name, item in dimensions.items() if item.state in {"missing", "penalized"}]
    uncertain = [name for name, item in dimensions.items() if item.state == "unknown"]
    hard_codes = [f"{name}_incompatible" for name, item in dimensions.items() if item.state == "missing"]
    total_weight = sum(item.weight for item in dimensions.values())
    earned = sum(
        item.weight if item.state == "matched" else item.weight * 0.5 if item.state == "unknown" else 0
        for item in dimensions.values()
    )
    value = round(100 * earned / total_weight) if total_weight else 50
    if hard_codes:
        value = 0
        band: MatchBand = "ineligible"
    elif value >= 85:
        band = "strong"
    elif value >= 70:
        band = "good"
    else:
        band = "borderline"
    return MatchScore(
        value=value,
        band=band,
        dimensions=dimensions,
        matched_requirements=matched,
        missing_requirements=missing,
        uncertain_requirements=uncertain,
        hard_incompatibilities=hard_codes,
    )


__all__ = ["SCORE_VERSION", "MatchDimension", "MatchScore", "compute_match_score"]
