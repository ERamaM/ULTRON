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
        ("Leuven", False),
        ("Leuven, Belgium", True),  # Belgium is an EU country; "eu" inside "Leuven" still does not count
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


@pytest.mark.parametrize("region", ["Leuven", "Neuquén"])
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


NON_EU_REMOTE_SCOPES = [
    "Argentina", "Chile", "Colombia", "Perú", "Uruguay", "Brasil", "Vietnam", "South Korea", "Israel", "Turkey",
    "Saudi Arabia", "Indonesia", "Malaysia", "Thailand", "Nigeria", "Kenya", "South Africa", "Egypt", "LATAM",
    "Latin America", "APAC", "Estados Unidos", "EE.UU.", "EEUU", "Latinoamérica", "Corea del Sur", "Turquía",
]


@pytest.mark.parametrize("place", NON_EU_REMOTE_SCOPES)
def test_remote_from_a_non_eu_place_only_is_a_red_flag(place):
    assert has_region_red_flags(f"Fully remote from {place} only") is True
    assessment = assess_geography(
        location="", remote_text=f"Remote from {place} only", body_text="Build ML systems.",
        target_regions=["Remote EU"],
    )
    assert assessment.conflict is True


def test_generic_global_wording_does_not_unflag_a_non_eu_region():
    assert has_region_red_flags("Remote LATAM only. Join our global team, work from anywhere in the region.") is True


def test_spanish_us_residence_requirement_is_a_red_flag():
    assert has_region_red_flags("Remoto solo desde Estados Unidos") is True
    assert has_region_red_flags("Ubicación: EE.UU. (remoto)") is True


@pytest.mark.parametrize(
    "text",
    [
        "Remote EMEA, APAC, LATAM",
        "Remote (EMEA or APAC)",
        "Remote: open to candidates in Spain, Germany or Argentina",
        "Remote from Portugal or Brazil",
        "Remote Europe. Customers in Chile and Kenya.",
        "Remote within the EU; our founders are from Israel.",
        "We have offices in Bogotá, Colombia. Remote Europe.",
        "Remote worldwide, APAC time zones preferred.",
    ],
)
def test_mixed_european_and_non_eu_scopes_are_not_red_flags(text):
    assert has_region_red_flags(text) is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Fully remote from Switzerland only", False),
        ("Remote from Norway only", False),
        ("Remote - EEA", False),
        ("Remote from Germany only", False),
        ("UK only", True),
    ],
)
def test_european_non_eu_countries_keep_their_existing_handling(text, expected):
    assert has_region_red_flags(text) is expected


@pytest.mark.parametrize(
    ("location", "remote_text"),
    [("Neuquén, Argentina", "Fully remote from Argentina only"), ("Estados Unidos", "Remoto solo desde Estados Unidos")],
)
def test_hard_filter_discards_non_eu_remote_scope_for_european_targets(location, remote_text):
    from ultron.core.models import ExtractedJob
    from ultron.core.rules.hard_filters import apply_hard_filters

    job = ExtractedJob(
        apply_url="https://boards.greenhouse.io/acme/jobs/1", domain="boards.greenhouse.io",
        title="Senior ML Engineer", location=location, remote_text=remote_text,
        body_text="Build machine learning systems. Applications open.",
    )
    assert apply_hard_filters(job, ["greenhouse.io"], target_regions=["Spain", "Remote EU"]) == (
        False, "non_eu_remote"
    )
    assert apply_hard_filters(job, ["greenhouse.io"], target_regions=["Remote LATAM"], geo="other") == (True, None)


def test_worldwide_eligibility_overrides_scoped_non_eu_countries():
    from ultron.core.rules.geo_eu import has_region_red_flags

    assert has_region_red_flags("Remote worldwide, including Argentina.") is False
    assert has_region_red_flags("Location: Remote worldwide; candidates in Israel welcome.") is False
    assert has_region_red_flags("Location: Fully remote from Argentina only.") is True
