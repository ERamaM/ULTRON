"""Final-row assembly, ported literally from MARK III ``pipeline/evaluation.py``.

``resolve_geo_defaults`` and ``_assemble_final_row`` are unchanged except imports and the
``geo`` scope argument (``"eu"``, the default, is MARK III's behaviour).
``prepare_geography``, ``target_regions`` and ``fallback_fit_signals`` hold the caller-side
computation that MARK III performs in ``pipeline/verification_gates.py::apply_pending_evaluation_gates``
and ``pipeline/engine.py::_profile_matching_policy`` (same expressions, no run object).
"""

from __future__ import annotations

from typing import Any

from ultron.core.models import (
    COMPANY_NOT_FOUND,
    LOCATION_NOT_SHOWN,
    TITLE_NOT_FOUND,
    CandidateUrl,
    ExtractedJob,
    FinalRow,
    GeoScope,
)
from ultron.core.ranker.match_score import compute_match_score
from ultron.core.ranker.profile_terms import collect_profile_term_hits
from ultron.core.remote_requirement import is_remote_required
from ultron.core.rules.geo_eu import SPAIN_LOCALITY_TERMS
from ultron.core.rules.geography import (
    GeoAssessment,
    assess_geography,
    build_geo_body_excerpt,
    location_scope,
    scoped_eligibility_terms,
    scoped_posting_eligibility,
)
from ultron.core.rules.hard_filters import (
    classify_remote_policy,
    location_excluded,
)
from ultron.core.rules.term_matching import find_terms


def resolve_geo_defaults(
    remote_policy: str | None, eligibility: str | None, *, geo: GeoScope = "eu", remote_required: bool = True
) -> tuple[str, str]:
    """Fill geo fields from each other without inventing EMEA eligibility.

    Outside a European scope nothing is inferred: a missing field is ``"unknown"``.
    When ``remote_required`` is False, eligibility never implies a remote policy.
    """
    if geo != "eu":
        return remote_policy or "unknown", eligibility or "unknown"
    if remote_policy is None and eligibility is not None and remote_required:
        if eligibility == "confirmed":
            remote_policy = "remote_spain"
        elif eligibility == "high":
            remote_policy = "remote_eu"
        elif eligibility == "unknown":
            remote_policy = "unknown"
        else:
            remote_policy = "remote_emea"
    if eligibility is None and remote_policy is not None:
        if remote_policy == "remote_spain":
            eligibility = "confirmed"
        elif remote_policy in {"remote_eu", "remote_europe"}:
            eligibility = "high"
        elif remote_policy == "unknown":
            eligibility = "unknown"
        else:
            eligibility = "medium"
    if remote_policy is None and eligibility is None:
        remote_policy, eligibility = "unknown", "unknown"
    elif remote_policy is None:
        remote_policy = "unknown"
    elif eligibility is None:
        eligibility = "unknown"
    return remote_policy, eligibility


def _assemble_final_row(
    extracted: ExtractedJob,
    evaluation: Any,
    *,
    heur_remote_policy: str | None,
    heur_eligibility: str | None,
    geo_red_flags: bool,
    geo_risk_note: str,
    candidate: CandidateUrl,
    fallback_fit_signals: list[str] | None = None,
    profile_criteria: dict[str, Any] | None = None,
    geo: GeoScope = "eu",
) -> FinalRow:
    """Build a kept FinalRow: fill geo defaults, assemble eligibility evidence, map source fields."""
    remote_required = is_remote_required(profile_criteria)
    remote_policy = heur_remote_policy
    eligibility = heur_eligibility
    if (
        eligibility is None
        and geo == "eu"
        and not remote_required
        and not geo_red_flags
        and find_terms(f"{extracted.location}\n{extracted.remote_text}", SPAIN_LOCALITY_TERMS)
    ):
        eligibility = "confirmed"
    if eligibility is None and evaluation.eligibility is not None and not geo_red_flags:  # noqa: SIM102 - literal port
        # LLM can only fill gaps when geo heuristics do not indicate a conflict.
        if (
            remote_policy is not None
            or evaluation.eligibility == "medium"
            or not remote_required
            or getattr(evaluation, "evaluation_source", None) == "agent"
        ):
            eligibility = evaluation.eligibility

    remote_policy, eligibility = resolve_geo_defaults(
        remote_policy, eligibility, geo=geo, remote_required=remote_required
    )
    eligibility_evidence_parts = [
        part
        for part in (
            evaluation.eligibility_evidence or extracted.remote_text or extracted.location,
            geo_risk_note,
        )
        if part
    ]
    eligibility_evidence = " | ".join(dict.fromkeys(eligibility_evidence_parts))

    score = (
        compute_match_score(
            evaluation,
            profile_criteria,
            source=extracted,
            term_hits=collect_profile_term_hits(extracted, profile_criteria),
        )
        if profile_criteria is not None
        else None
    )
    return FinalRow(
        title=extracted.title or TITLE_NOT_FOUND,
        company=extracted.company or COMPANY_NOT_FOUND,
        apply_url=extracted.apply_url,
        location=extracted.location or LOCATION_NOT_SHOWN,
        remote_policy=remote_policy,
        eligibility=eligibility,
        eligibility_evidence=eligibility_evidence,
        fit_signals=(evaluation.fit_signals or fallback_fit_signals or [])[:3],
        freshness=extracted.posting_date or "date_not_shown (open verified)",
        evaluation_verdict=evaluation.verdict,
        source_type=candidate.source_type or "unknown",
        source_key=candidate.source_key or candidate.query_id,
        source_label=candidate.source_label or candidate.query or "Unknown source",
        match_score=score.value if score else None,
        match_band=score.band if score else None,
        score_version=score.version if score else "",
        match_dimensions=(
            {name: dimension.model_dump(mode="json") for name, dimension in score.dimensions.items()} if score else {}
        ),
        matched_requirements=score.matched_requirements if score else [],
        missing_requirements=score.missing_requirements if score else [],
        uncertain_requirements=score.uncertain_requirements if score else [],
    )


assemble_final_row = _assemble_final_row


def target_regions(criteria: dict[str, Any]) -> list[str]:
    """MARK III ``_profile_matching_policy``: preferred regions of ``location_scope``."""
    location_scope = criteria.get("location_scope")
    return list(location_scope.get("preferred") or []) if isinstance(location_scope, dict) else []


def fallback_fit_signals(criteria: dict[str, Any]) -> list[str]:
    """MARK III caller expression: ``criteria.fallback_fit_signals`` or the role focus terms, first three."""
    return list(criteria.get("fallback_fit_signals") or criteria.get("role_focus_terms") or [])[:3]


def prepare_geography(
    extracted: ExtractedJob, criteria: dict[str, Any], *, recall_active: bool = False, geo: GeoScope = "eu"
) -> tuple[GeoAssessment, str, str | None]:
    """Post-evaluation geography gate (MARK III ``apply_pending_evaluation_gates``).

    Returns ``(geography, geo_risk_note, discard_reason)``. European scope: a geo conflict
    discards the role (``non_eu_remote``) unless recall mode softens it into a
    ``geo_risk_non_eu`` note. Other scopes: no European rules; only the profile's
    ``location_scope.excluded_terms`` discard (``location_excluded``).
    """
    if geo != "eu":
        return _scoped_geography(extracted, criteria, geo)
    geography = assess_geography(
        location=extracted.location,
        remote_text=extracted.remote_text,
        body_text=extracted.body_text,
        target_regions=target_regions(criteria),
    )
    geo_risk_note = ""
    if geography.conflict:
        if recall_active:
            geo_risk_note = "geo_risk_non_eu"
        else:
            return geography, geo_risk_note, "non_eu_remote"
    return geography, geo_risk_note, None


def _scoped_geography(
    extracted: ExtractedJob, criteria: dict[str, Any], geo: GeoScope
) -> tuple[GeoAssessment, str, str | None]:
    scope = location_scope({"criteria": criteria})
    geo_text = f"{extracted.location}\n{extracted.remote_text}\n{build_geo_body_excerpt(extracted.body_text)}"
    geography = GeoAssessment(
        european_scope=False,
        red_flags=False,
        remote_policy=classify_remote_policy(
            geo_text, geo, remote_text=extracted.remote_text, body_text=extracted.body_text
        ),
        eligibility=scoped_posting_eligibility(
            extracted.location, extracted.remote_text, extracted.body_text, scoped_eligibility_terms({"criteria": criteria})
        ),
        conflict=False,
    )
    full_text = f"{extracted.location}\n{extracted.remote_text}\n{extracted.body_text}"
    return geography, "", "location_excluded" if location_excluded(full_text, scope["excluded_terms"]) else None
