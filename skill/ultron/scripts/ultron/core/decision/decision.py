"""Post-evaluation verdict normalization and heuristic conflict safeguards."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import Any

from ultron.core.models import GeoScope, JobEvaluation
from ultron.core.rules.geo_eu import has_region_red_flags

logger = logging.getLogger(__name__)

VALID_VERDICTS = {"keep", "discard", "unsure"}
VALID_CATEGORIES = {"seniority", "role_family", "remote_policy", "eligibility_scope", "closed_or_invalid", "other"}
VALID_CONFIDENCE = {"low", "medium", "high"}

CATEGORY_TO_REASON = {
    "seniority": "seniority_not_senior",
    "role_family": "non_matching_role_family",
    "remote_policy": "hybrid_or_not_explicit_remote",
    "eligibility_scope": "missing_eu_spain_eligibility",
    "closed_or_invalid": "expired_or_closed",
    "other": "llm_rejected",
}

REASON_TO_CATEGORY = {
    "seniority_not_senior": "seniority",
    "non_matching_role_family": "role_family",
    "hybrid_or_not_explicit_remote": "remote_policy",
    "missing_eu_spain_eligibility": "eligibility_scope",
    "non_eu_remote": "eligibility_scope",
    "location_excluded": "eligibility_scope",
    "location_ineligible": "eligibility_scope",
    "expired_or_closed": "closed_or_invalid",
    "apply not available": "closed_or_invalid",
}


def normalize_verdict(value: str | None) -> str:
    """Normalize a verdict string to ``keep``, ``discard``, or ``unsure``."""
    low = str(value or "").strip().lower()
    if low in VALID_VERDICTS:
        return low
    if low in {"accept", "pass", "approve"}:
        return "keep"
    if low in {"reject", "drop", "deny"}:
        return "discard"
    return "unsure"


def normalize_discard_category(category: str | None, reason: str | None) -> str | None:
    """Normalize a discard category name using reason hints as fallback."""
    if category:
        low = str(category).strip().lower().replace("-", "_").replace(" ", "_")
        low = {
            "family": "role_family",
            "role": "role_family",
            "remote": "remote_policy",
            "hybrid": "remote_policy",
            "eligibility": "eligibility_scope",
            "closed": "closed_or_invalid",
            "open_status": "closed_or_invalid",
        }.get(low, low)
        if low in VALID_CATEGORIES:
            return low
    mapped = REASON_TO_CATEGORY.get(str(reason or "").strip(), "")
    if mapped in VALID_CATEGORIES:
        return mapped
    return "other" if reason else None


def normalize_confidence(value: str | None) -> str:
    """Normalize a confidence label to a valid value, defaulting to ``medium``."""
    low = str(value or "").strip().lower()
    return low if low in VALID_CONFIDENCE else "medium"


class DecisionFinalizer:
    """Applies the existing verdict normalization and recall guardrails."""

    def __init__(self, cfg: Any, *, is_llama_cpp: Callable[[], bool]) -> None:
        self.cfg = cfg
        self._is_llama_cpp = is_llama_cpp

    def finalize(
        self, evaluation: JobEvaluation, *, from_llm: bool, heuristic_baseline: JobEvaluation | None = None
    ) -> JobEvaluation:
        """Normalize an evaluation verdict and reconcile model discards against heuristics."""
        verdict = normalize_verdict(evaluation.verdict)
        category = normalize_discard_category(evaluation.discard_category, evaluation.discard_reason)
        evaluation.verdict = verdict
        evaluation.discard_category = category
        evaluation.confidence = normalize_confidence(evaluation.confidence)
        if not from_llm:
            evaluation.evaluation_source = "heuristic_fallback"
        if verdict == "keep":
            evaluation.keep = True
            evaluation.discard_reason = None
            return evaluation
        if verdict == "discard":
            if self._should_soften_llm_discard(evaluation, category, heuristic_baseline, from_llm):
                return self._soften(evaluation, category)
            if self._is_technical_hybrid(evaluation, category, from_llm):
                evaluation.keep = True
                evaluation.verdict = "keep"
                evaluation.discard_reason = None
                evaluation.is_full_remote = True
                evaluation.evaluation_source = "llm_plus_guardrail"
                return evaluation
            evaluation.keep = False
            evaluation.discard_reason = canonical_reason(
                category, evaluation.discard_reason, geo=getattr(self.cfg, "geo", None) or "eu"
            )
            return evaluation
        evaluation.keep = False
        evaluation.discard_reason = None
        return evaluation

    def _should_soften_llm_discard(
        self, evaluation: JobEvaluation, category: str | None, baseline: JobEvaluation | None, from_llm: bool
    ) -> bool:
        return (
            from_llm
            and self.cfg.recall_mode_active
            and self._is_llama_cpp()
            and category == "eligibility_scope"
            and not heuristic_agrees_with_llm_discard(category, baseline)
            and not has_clear_non_eu_scope(evaluation, getattr(self.cfg, "geo", None) or "eu")
        )

    @staticmethod
    def _soften(evaluation: JobEvaluation, category: str | None) -> JobEvaluation:
        evaluation.verdict = "unsure"
        evaluation.keep = False
        evaluation.discard_reason = None
        evaluation.discard_category = None
        evaluation.evaluation_source = "llm_plus_guardrail"
        logger.info(
            "evaluator local_recall_soften category=%s conf=%s source=%s",
            category,
            evaluation.confidence,
            evaluation.evaluation_source,
        )
        return evaluation

    @staticmethod
    def _is_technical_hybrid(evaluation: JobEvaluation, category: str | None, from_llm: bool) -> bool:
        if not (from_llm and evaluation.confidence in {"low", "medium"} and category == "remote_policy"):
            return False
        remote_text = (evaluation.remote_evidence or "").lower()
        if "remote" not in remote_text or re.search(r"hybrid\s+\d+\s+day", remote_text):
            return False
        return any(
            term in remote_text
            for term in (
                "hybrid cloud",
                "hybrid search",
                "hybrid ai",
                "hybrid model architecture",
                "hybrid model training",
            )
        )


def has_clear_non_eu_scope(evaluation: JobEvaluation, geo: GeoScope = "eu") -> bool:
    """European-scope only: the evidence shows a clear non-EU restriction."""
    if geo != "eu":
        return False
    return has_region_red_flags(
        "\n".join(
            part
            for part in (evaluation.eligibility_evidence, evaluation.remote_evidence, evaluation.open_status_evidence)
            if part
        )
    )


def heuristic_agrees_with_llm_discard(category: str | None, baseline: JobEvaluation | None) -> bool:
    """Return True if the heuristic baseline produced the same discard category."""
    if category is None or baseline is None:
        return False
    return normalize_discard_category(baseline.discard_category, baseline.discard_reason) == category


def canonical_reason(category: str | None, reason: str | None, *, geo: GeoScope = "eu") -> str:
    """Map a discard category and sub-reason to its canonical export code."""
    # A profile-excluded location keeps its own reason instead of the EU eligibility one,
    # and outside a European scope an eligibility discard gets a neutral label.
    if category == "eligibility_scope" and str(reason or "").strip() == "location_excluded":
        return "location_excluded"
    if category == "eligibility_scope" and geo != "eu":
        return "location_ineligible"
    if category in CATEGORY_TO_REASON:
        return CATEGORY_TO_REASON[category]
    raw = str(reason or "").strip()
    return raw if raw in REASON_TO_CATEGORY else (raw or "llm_rejected")
