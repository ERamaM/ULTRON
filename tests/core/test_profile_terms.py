"""Configured terms have bounded sources and only score penalties."""

import pytest

from ultron.core.models import ExtractedJob
from ultron.core.ranker.match_score import compute_match_score
from ultron.core.ranker.profile_terms import collect_profile_term_hits, exclusion_lists_configured
from ultron.core.rules.role_matching import is_senior_title


def job(**values):
    return ExtractedJob(apply_url="https://example.test/job", domain="example.test", **values)


@pytest.mark.parametrize(
    ("section", "field", "hit", "source"),
    [
        ("", "exclusion_terms", "exclusion_terms", "title"),
        ("seniority", "excluded_terms", "seniority_excluded", "title"),
        ("remote_policy", "negative_terms", "remote_negative", "remote_text"),
        ("remote_policy", "positive_terms", "remote_positive", "location"),
        ("location_scope", "excluded_terms", "location_excluded", "location"),
        ("location_scope", "eligibility_terms", "location_eligibility", "remote_text"),
    ],
)
def test_terms_match_accents_and_boundaries_in_permitted_sources(section, field, hit, source):
    criteria = {section: {field: ["tecnico"]}} if section else {field: ["tecnico"]}
    assert getattr(collect_profile_term_hits(job(**{source: "Técnico"}), criteria), hit) == ("tecnico",)
    assert not getattr(collect_profile_term_hits(job(**{source: "técnicos"}), criteria), hit)


def test_body_and_other_fields_cannot_leak_into_penalties():
    criteria = {
        "exclusion_terms": ["manager"],
        "seniority": {"excluded_terms": ["junior"]},
        "remote_policy": {"negative_terms": ["hybrid"]},
    }
    assert not collect_profile_term_hits(
        job(body_text="Work with your manager, junior colleagues and hybrid cloud."), criteria
    ).penalties
    assert not collect_profile_term_hits(job(remote_text="junior", title="hybrid"), criteria).penalties
    assert collect_profile_term_hits(
        job(body_text="Location: Canada"), {"location_scope": {"excluded_terms": ["Canada"]}}
    ).location_excluded == ("Canada",)


@pytest.mark.parametrize(
    "criteria",
    [{}, {"seniority": "junior"}, {"exclusion_terms": "manager"}, {"remote_policy": {"negative_terms": [None, 3]}}],
)
def test_empty_or_malformed_lists_are_not_configured(criteria):
    assert not exclusion_lists_configured(criteria)
    assert not collect_profile_term_hits(job(), criteria).penalties


def test_exclusions_lower_score_without_changing_verdict_or_hard_incompatibilities():
    criteria = {"exclusion_terms": ["manager"], "seniority": {"excluded_terms": ["manager"]}}
    evaluation = {"verdict": "keep", "keep": True, "is_allowed_family": True, "open_status_ok": True}
    source = job(title="Manager")
    hits = collect_profile_term_hits(source, criteria)
    penalized = compute_match_score(evaluation, criteria, source=source, term_hits=hits)
    clean = compute_match_score(
        evaluation, criteria, source=source, term_hits=collect_profile_term_hits(job(), criteria)
    )
    unknown = compute_match_score(evaluation, criteria, source=source)
    absent = compute_match_score(evaluation, {}, source=source)
    assert hits.penalties == ("manager",)
    assert penalized.value < clean.value
    assert penalized.dimensions["exclusions"].state == "penalized"
    assert clean.dimensions["exclusions"].state == "matched"
    assert unknown.dimensions["exclusions"].state == "unknown"
    assert absent.dimensions["exclusions"].state == "not_required"
    assert penalized.missing_requirements == ["exclusions"]
    assert not penalized.hard_incompatibilities
    assert penalized.band != "ineligible"
    assert penalized.version == "score-v4"
    assert evaluation["verdict"] == "keep" and evaluation["keep"]


@pytest.mark.parametrize(
    ("remote", "body", "state"),
    [
        ("distributed", "", "matched"),
        ("distributed hybrid", "", "unknown"),
        ("distributed", "Mandatory onsite attendance", "unknown"),
    ],
)
def test_remote_positive_upgrade_requires_no_negative_or_onsite(remote, body, state):
    criteria = {
        "remote_policy": {"required": "full_remote", "positive_terms": ["distributed"], "negative_terms": ["hybrid"]},
        "location_scope": {"preferred": ["Spain"], "eligibility_terms": ["Spain"]},
    }
    source = job(remote_text=remote, body_text=body, location="Spain")
    result = compute_match_score({}, criteria, source=source, term_hits=collect_profile_term_hits(source, criteria))
    assert result.dimensions["remote"].state == state
    assert result.dimensions["eligibility"].state == "unknown"
    assert "Location keywords: Spain" in result.dimensions["eligibility"].evidence


def test_configured_seniority_replaces_default_levels():
    assert is_senior_title("Mid ML Engineer", level_terms=["mid"])
    assert not is_senior_title("Senior ML Engineer", level_terms=["mid"])
    assert not is_senior_title("Mid Junior ML Engineer", level_terms=["mid"], excluded_level_terms=["junior"])
    assert is_senior_title("Distinguished ML Engineer")
