import pytest

from ultron.core.rules.geography import assess_geography, is_european_scope


@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("Remote EU", True),
        ("Spain", True),
        ("Remote EMEA", True),
        ("Europa", True),
        ("España", True),
        ("Leuven, Belgium", False),
        ("Neuquén", False),
        ("Remote LATAM", False),
        ("Seoul", False),
        ("", False),
    ],
)
def test_scope_uses_whole_normalized_tokens(region, expected):
    assert is_european_scope([region]) is expected


@pytest.mark.parametrize("european", [False, True])
@pytest.mark.parametrize("red_flags", [False, True])
@pytest.mark.parametrize("hint", [None, "high"])
def test_geography_conflict_truth_table(european, red_flags, hint):
    assessment = assess_geography(
        location="US only" if red_flags else "",
        remote_text="",
        body_text="Build products",
        target_regions=["Remote EU"] if european else ["Leuven"],
        eligibility_hint=hint,
    )
    assert assessment.european_scope is european
    assert assessment.red_flags is red_flags
    assert assessment.conflict is (european and red_flags and hint is None)


def test_positive_source_signal_overrides_conflict():
    result = assess_geography(location="", remote_text="Remote EU", body_text="US only", target_regions=["EU"])
    assert result.remote_policy == "remote_eu"
    assert result.eligibility == "high"
    assert not result.conflict


@pytest.mark.parametrize("region", ["Leuven, Belgium", "Neuquén"])
def test_heuristic_does_not_infer_europe_from_partial_region_name(region):
    from types import SimpleNamespace

    from ultron.core.decision.heuristics import HeuristicEvaluator

    evaluator = HeuristicEvaluator(SimpleNamespace(evaluation_profile=SimpleNamespace(target_regions=(region,))))
    assert evaluator._eligibility("Remote US only", None) == "medium"
