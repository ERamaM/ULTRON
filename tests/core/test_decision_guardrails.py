"""Technical-hybrid guardrail: only a technical "hybrid" phrase turns an agent remote-policy discard into a keep."""

from __future__ import annotations

import pytest

from ultron.core.decision.decision import DecisionFinalizer
from ultron.core.decision.heuristics import HeuristicConfig
from ultron.core.models import JobEvaluation


def _evaluation(evidence: str, confidence: str = "medium") -> JobEvaluation:
    return JobEvaluation(
        keep=False, verdict="discard", discard_category="remote_policy",
        discard_reason="hybrid_or_not_explicit_remote", confidence=confidence, is_senior=True,
        is_allowed_family=True, is_full_remote=False, remote_evidence=evidence,
    )


def _finalizer() -> DecisionFinalizer:
    return DecisionFinalizer(HeuristicConfig(recall_mode_active=False, geo="eu"), is_llama_cpp=lambda: False)


def test_hybrid_work_model_stays_discarded():
    result = _finalizer().finalize(_evaluation("We operate a hybrid model; remote some days."), from_llm=True)
    assert (result.verdict, result.keep) == ("discard", False)


def test_technical_hybrid_phrase_turns_a_remote_discard_into_a_keep():
    result = _finalizer().finalize(_evaluation("Fully remote role building hybrid search infrastructure."), from_llm=True)
    assert (result.verdict, result.keep, result.is_full_remote) == ("keep", True, True)
    assert result.evaluation_source == "llm_plus_guardrail"


@pytest.mark.parametrize(
    ("evidence", "confidence", "from_llm"),
    [
        ("Fully remote role building hybrid search infrastructure.", "high", True),
        ("Fully remote role building hybrid search infrastructure.", "medium", False),
        ("Remote role on hybrid cloud, hybrid 3 days in office.", "low", True),
        ("Hybrid search team, office based.", "low", True),
    ],
)
def test_guardrail_needs_low_confidence_agent_evidence_with_remote_and_no_office_days(evidence, confidence, from_llm):
    result = _finalizer().finalize(_evaluation(evidence, confidence), from_llm=from_llm)
    assert (result.verdict, result.keep) == ("discard", False)
