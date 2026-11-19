import pytest

from ultron.core.rules.geo_eu import _has_us_location_signal, has_region_red_flags
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
    from ultron.core.models import ExtractedJob

    evaluator = HeuristicEvaluator(SimpleNamespace(evaluation_profile=SimpleNamespace(target_regions=(region,))))
    job = ExtractedJob(apply_url="https://example.test/job/1", domain="example.test", remote_text="Remote US only")
    assert evaluator._eligibility(job, None) == "medium"


@pytest.mark.parametrize(
    "body",
    [
        "Previous remote experience is a plus.",
        "We hold numerous remote offsites every year.",
        "Bonus based on performance.",
    ],
)
def test_us_explicit_terms_do_not_match_inside_words(body):
    assert has_region_red_flags(f"Remote\n{body}") is False
    assessment = assess_geography(location="", remote_text="Remote", body_text=body, target_regions=["Remote Europe"])
    assert assessment.red_flags is False
    assert assessment.conflict is False


@pytest.mark.parametrize(
    "body",
    [
        "US remote only.",
        "This is a US-based role.",
        "This role is U.S. (Remote).",
        "This is a _US-based_ role.",
        "Location: U.S.A.",
        "Location: U.S.A",
    ],
)
def test_us_explicit_terms_still_flag_whole_phrases(body):
    assert has_region_red_flags(f"Remote\n{body}") is True


@pytest.mark.parametrize("text", ["Location: U.S.A.", "location: u.s.a", "Location: U.S."])
def test_dotted_us_location_label_sets_us_location_signal(text):
    assert _has_us_location_signal(text.lower()) is True
