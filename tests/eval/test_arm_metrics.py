"""Arm comparison metrics: precision at k, rates, pooled cost and reproducibility."""

from __future__ import annotations

import arm_metrics as am
import pytest


def _labels(relevant=(), irrelevant=(), unknown=()):
    labels = {job: {"relevant": True} for job in relevant}
    labels |= {job: {"relevant": False} for job in irrelevant}
    return labels | {job: {"relevant": None} for job in unknown}


def test_ratio_jaccard_unique():
    assert am.ratio(1, 0) is None
    assert am.jaccard(set(), set()) is None
    assert am.jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(0.3333)
    assert am.unique_in_order(["a", "b", "a"]) == ["a", "b"]


def test_precision_at_never_credits_jobs_below_the_cutoff():
    labels = _labels(relevant="cf", irrelevant="ab")
    assert am.precision_at(["a", "b", "c", "f"], labels, 2) == 0.0


def test_precision_at_counts_repeats_and_empty_slots_as_misses():
    labels = _labels(relevant="a")
    assert am.precision_at(["a", "a"], labels, 2) == 0.5
    assert am.precision_at(["a"], labels, 5) == 0.2


def test_precision_at_skips_unlabelled_positions():
    labels = _labels(relevant="a", unknown="u")
    assert am.precision_at(["u", "a"], labels, 2) == 1.0
    assert am.precision_at(["u"], labels, 1) is None
    assert am.precision_at(["u"], labels, 2) == 0.0  # the empty position is still a miss


def test_precision_at_scores_an_empty_shortlist_as_zero():
    assert am.precision_at([], {}, 5) == 0.0


def test_rep_metrics_with_known_values():
    labels = {"a": {"relevant": True, "open": True, "eligible": True, "role_fit": True},
              "b": {"relevant": False, "open": False, "eligible": False, "role_fit": False},
              "c": {"relevant": True, "open": True, "eligible": None, "role_fit": True}}
    metrics = am.rep_metrics(["a", "b", "a", "c"], labels, {"tokens": 1000, "seconds": 60}, {"a", "c", "z"})
    assert metrics["duplicate_rows"] == 1
    assert metrics["precision"] == pytest.approx(0.6667)
    assert metrics["precision_at_5"] == 0.4  # a, b, a(repeat), c, empty slot
    assert metrics["closed_rate"] == pytest.approx(0.3333)
    assert metrics["ineligible_rate"] == 0.5
    assert metrics["role_mismatch_rate"] == pytest.approx(0.3333)
    assert metrics["pool_recall"] == pytest.approx(0.6667)
    assert metrics["tokens"] == 1000 and metrics["tool_calls"] is None


def test_mean_std_uses_sample_std():
    assert am.mean_std([None]) == {"mean": None, "std": None, "n": 0}
    assert am.mean_std([2.0]) == {"mean": 2.0, "std": None, "n": 1}
    assert am.mean_std([1.0, 3.0, None]) == {"mean": 2.0, "std": pytest.approx(1.4142), "n": 2}


def test_reproducibility():
    assert am.reproducibility([{"a"}]) is None
    assert am.reproducibility([{"a", "b"}, {"a", "b"}]) == 1.0
    assert am.reproducibility([set(), set()]) is None


def test_pooled_cost_counts_failed_repetitions():
    per_rep = {"1": {"relevant": 0, "tokens": 9000}, "2": {"relevant": 3, "tokens": 3000},
               "3": {"relevant": 2, "tokens": None}}
    assert am.pooled_cost_per_relevant(per_rep, "tokens") == 4000
    assert am.pooled_cost_per_relevant({"1": {"relevant": 0, "tokens": 5}}, "tokens") is None


def test_compare_arms_counts_exclusive_overlap_and_reproducibility():
    labels = _labels(relevant="abc", irrelevant="d")
    arms = {"ultron": {"1": ["a", "b"], "2": ["a", "d"]}, "baseline": {"1": ["c", "a"], "2": ["c"]}}
    costs = {"baseline": {"1": {"tokens": 100}, "2": {"tokens": 100}}}
    result = am.compare_arms(arms, labels, costs)
    assert result["pool"] == {"jobs": 4, "relevant": 3}
    ultron, baseline = result["arms"]["ultron"], result["arms"]["baseline"]
    assert ultron["only_this_arm_relevant"] == 1 and baseline["only_this_arm_relevant"] == 1
    assert ultron["reproducibility_jaccard"] == pytest.approx(0.3333)
    assert ultron["reproducibility_relevant_jaccard"] == 0.5
    assert baseline["cost_per_relevant"]["tokens"] == pytest.approx(66.67)
    assert ultron["zero_relevant_repetitions"] == 0
    assert result["between_arms"]["baseline|ultron"]["jaccard_relevant"] == pytest.approx(0.3333)
