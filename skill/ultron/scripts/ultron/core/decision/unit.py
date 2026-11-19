"""Single-job heuristic decision unit.

Port of MARK III ``tools/generate_heuristic_characterization.py`` (``_evaluator``,
``characterize_case`` and ``_decision_stage``) without the prompt part. The parity
test and the ``filter`` stage both run this exact code.
"""

from __future__ import annotations

from typing import Any

import yaml

from ultron.core.data_paths import data_dir
from ultron.core.decision.decision import DecisionFinalizer
from ultron.core.decision.heuristics import HeuristicConfig, HeuristicEvaluator
from ultron.core.models import ExtractedJob, GeoScope, JobEvaluation
from ultron.core.remote_requirement import is_remote_required
from ultron.core.rules.classification_signals import build_classification_signals
from ultron.core.rules.geography import geo_scope, scoped_eligibility_terms
from ultron.core.rules.hard_filters import apply_hard_filters, pre_filter_candidate
from ultron.core.rules.role_matching import RoleNegatives, role_negatives_for
from ultron.core.vocabulary import VocabularyPack, active_pack


def load_allowed_domains() -> list[str]:
    """Return ``allowed_ats_domains`` from the bundled ``domain_rules.yaml``."""
    rules = yaml.safe_load((data_dir() / "domain_rules.yaml").read_text(encoding="utf-8")) or {}
    return [str(domain) for domain in rules.get("allowed_ats_domains") or []]


def _role_terms(profile: dict[str, Any], vocabulary: VocabularyPack) -> tuple[str, ...]:
    """Profile role terms: ``role_focus_terms``; else, only when the pack has no family default, ``target_titles``.

    With a pack, empty role terms keep MARK III behaviour (the pack's ``role_terms`` are the
    default family). Without one there is no default, so a profile that only lists target
    titles must be matched by those titles.
    """
    criteria = profile.get("criteria") or {}
    focus = tuple(criteria.get("role_focus_terms") or ())
    if vocabulary.role_terms:
        return focus
    titles = tuple(criteria.get("target_titles") or ())
    return tuple(dict.fromkeys((*focus, *titles)))


def _config(
    profile: dict[str, Any], vocabulary: VocabularyPack, geo: GeoScope, role_negatives: RoleNegatives
) -> HeuristicConfig:
    return HeuristicConfig(
        role_focus_terms=_role_terms(profile, vocabulary),
        search_profile_snapshot=profile,
        vocabulary=vocabulary,
        geo=geo,
        role_negatives=role_negatives,
    )


def _evaluate(cfg: HeuristicConfig, job: ExtractedJob, signals: Any) -> JobEvaluation:
    """MARK III ``JobEvaluator.evaluate`` without a model client: heuristics, then finalize."""
    evaluation = HeuristicEvaluator(cfg).evaluate(job, classification_signals=signals)
    return DecisionFinalizer(cfg, is_llama_cpp=lambda: False).finalize(evaluation, from_llm=False)


def _decision_stage(
    *,
    pre_keep: bool,
    pre_reason: str | None,
    evaluation: Any,
    hard_keep: bool,
    hard_reason: str | None,
) -> dict[str, Any]:
    if not pre_keep:
        return {"outcome": "discard", "reason": pre_reason, "stage": "pre_filter"}
    if not (evaluation.keep or evaluation.verdict == "unsure"):
        return {
            "outcome": "discard",
            "reason": evaluation.discard_reason,
            "stage": "heuristic_evaluator",
        }
    if not hard_keep:
        return {"outcome": "discard", "reason": hard_reason, "stage": "hard_filter"}
    return {
        "outcome": "review" if evaluation.verdict == "unsure" else "keep",
        "reason": None,
        "stage": "completed",
    }


def characterize_job(
    job: ExtractedJob,
    profile: dict[str, Any],
    allowed_domains: list[str],
    vocabulary: VocabularyPack | None = None,
    geo: GeoScope | None = None,
    role_negatives: RoleNegatives | None = None,
    *,
    allow_outside_ats_allowlist: bool = False,
    target_regions: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Characterize one job.

    ``vocabulary``, ``geo`` and ``role_negatives`` are derived from the profile when not
    given (callers looping over many jobs compute them once). With ``geo == "eu"`` this is
    MARK III's behaviour; other scopes skip the European geography rules.
    ``allow_outside_ats_allowlist`` is MARK III's flag of the same name, passed to the pre-filter
    and the hard filters (the caller sets it for ATS-API postings and ATS-fingerprinted pages).
    """
    if vocabulary is None:
        vocabulary = active_pack(profile)
    if geo is None:
        geo = geo_scope(profile)
    if role_negatives is None:
        role_negatives = role_negatives_for(profile)
    cfg = _config(profile, vocabulary, geo, role_negatives)
    role_terms = _role_terms(profile, vocabulary)
    signals = build_classification_signals(
        job,
        role_focus_terms=role_terms,
        role_signal_terms=tuple((profile.get("criteria") or {}).get("role_signal_terms") or ()),
        data_scientist_signal_terms=tuple((profile.get("criteria") or {}).get("data_scientist_signal_terms") or ()),
        role_ambiguity_terms=tuple((profile.get("criteria") or {}).get("role_ambiguity_terms") or ()),
        seniority_preferred_terms=(profile.get("criteria", {}).get("seniority") or {}).get("preferred_terms"),
        vocabulary=vocabulary,
        geo=geo,
        eligibility_terms=scoped_eligibility_terms(profile),
        role_negatives=role_negatives,
    )
    remote_required = is_remote_required(profile.get("criteria"))
    pre_keep, pre_reason = pre_filter_candidate(
        job,
        allowed_domains,
        role_focus_terms=role_terms,
        classification_signals=signals,
        remote_required=remote_required,
        target_regions=target_regions,
        vocabulary=vocabulary,
        geo=geo,
        role_negatives=role_negatives,
        allow_outside_ats_allowlist=allow_outside_ats_allowlist,
    )
    evaluation = _evaluate(cfg, job, signals)
    hard_keep, hard_reason = apply_hard_filters(
        job,
        allowed_domains,
        remote_required=remote_required,
        target_regions=target_regions,
        geo=geo,
        allow_outside_ats_allowlist=allow_outside_ats_allowlist,
    )

    return {
        "classification_signals": {
            "eligibility_hint": signals.eligibility_hint,
            "remote_conflict": signals.remote_conflict,
            "remote_scope_hint": signals.remote_scope_hint,
            "role_ambiguous": signals.role_ambiguous,
            "role_score": signals.role_score,
            "seniority_ambiguous": signals.seniority_ambiguous,
        },
        "hard_filter": {"keep": hard_keep, "reason": hard_reason},
        "heuristic_evaluator": {
            "discard_category": evaluation.discard_category,
            "discard_reason": evaluation.discard_reason,
            "eligibility": evaluation.eligibility,
            "fit_signals": evaluation.fit_signals,
            "is_allowed_family": evaluation.is_allowed_family,
            "is_full_remote": evaluation.is_full_remote,
            "is_senior": evaluation.is_senior,
            "keep": evaluation.keep,
            "open_status_ok": evaluation.open_status_ok,
            "verdict": evaluation.verdict,
        },
        "pipeline_characterization": _decision_stage(
            pre_keep=pre_keep,
            pre_reason=pre_reason,
            evaluation=evaluation,
            hard_keep=hard_keep,
            hard_reason=hard_reason,
        ),
        "pre_filter": {"keep": pre_keep, "reason": pre_reason},
        "_evaluation": evaluation,
        "_signals": signals,
    }
