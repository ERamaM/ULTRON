"""Vocabulary packs: role vocabulary is profile data, not core code."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ultron import profiles
from ultron.core.ats.clients.base import AtsPosting
from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.discovery.prefilter import passes_coarse_prefilter
from ultron.core.discovery.title_vocabulary import build_title_vocabulary
from ultron.core.models import ExtractedJob
from ultron.core.rules.role_matching import is_allowed_family, score_role_family
from ultron.core.rules.title_synonyms import expand_role_terms
from ultron.core.vocabulary import VocabularyPack, active_pack, load_packs

PROFILES = Path(__file__).resolve().parents[1] / "fixtures" / "profiles"


def _profile(slug: str) -> dict:
    return yaml.safe_load((PROFILES / f"{slug}.yaml").read_text(encoding="utf-8"))


def _passes(profile: dict, title: str) -> bool:
    pack = active_pack(profile)
    title_vocabulary = build_title_vocabulary(profile["criteria"], vocabulary=pack)
    posting = AtsPosting(title=title, apply_url="https://boards.greenhouse.io/x/jobs/1", location="Remote EU")
    return passes_coarse_prefilter(posting, title_vocabulary=title_vocabulary, vocabulary=pack)


def test_ai_pack_contains_role_terms():
    pack = load_packs(["ai-ml"])
    assert "machine learning" in " ".join(pack.role_terms)
    assert "machine learning engineer" in pack.role_terms
    assert "llm engineer" in pack.role_terms
    assert "machine learning" in pack.ambiguity_terms
    assert "llm" in pack.ambiguity_terms


def test_empty_pack_is_empty():
    pack = load_packs([])
    assert pack == VocabularyPack()
    assert not any(getattr(pack, name) for name in pack.__dataclass_fields__)


def test_unknown_pack_raises():
    with pytest.raises(ValueError):
        load_packs(["no-such-pack"])
    with pytest.raises(ValueError):
        load_packs(["../profiles/ai-ml-remote-eu"])


def test_load_packs_merges_without_duplicates():
    single = load_packs(["ai-ml"])
    assert load_packs(["ai-ml", "ai-ml"]) == single


def test_active_pack_reads_criteria():
    assert active_pack(_profile("ai-ml-remote-eu")) == load_packs(["ai-ml"])
    assert active_pack(_profile("finance-eu")) == VocabularyPack()
    assert active_pack({}) == VocabularyPack()


def test_finance_title_passes_prefilter():
    profile = _profile("finance-eu")
    assert _passes(profile, "Senior Financial Controller") is True
    assert _passes(profile, "Senior ML Engineer") is False


def test_ai_profile_still_admits_ai_titles():
    assert _passes(_profile("ai-ml-remote-eu"), "Senior ML Engineer") is True


def test_empty_pack_disables_ai_profile_logic():
    vocabulary = build_title_vocabulary({}, vocabulary=load_packs([]))
    assert vocabulary.ai_profile is False
    assert vocabulary.role_terms == ()


def test_finance_job_role_family_allowed():
    job = ExtractedJob(
        title="Financial Controller",
        company="Example",
        location="Remote",
        remote_text="Fully remote within the EU",
        body_text="Own the month-end close, IFRS reporting and group consolidation. Applications open.",
        apply_url="https://boards.greenhouse.io/example/jobs/1",
        domain="boards.greenhouse.io",
        apply_available=True,
        is_open_hint=True,
    )
    result = characterize_job(job, _profile("finance-eu"), load_allowed_domains())
    assert result["heuristic_evaluator"]["is_allowed_family"] is True


@pytest.mark.parametrize("slug", ["finance-eu", "backend-latam", "ai-ml-remote-eu"])
def test_fixture_profiles_are_valid(slug):
    assert profiles.validate_profile(_profile(slug)) == []


def test_profile_validation_rejects_unknown_pack():
    data = _profile("finance-eu")
    data["criteria"]["vocabulary_packs"] = ["no-such-pack"]
    assert any("vocabulary_packs" in error for error in profiles.validate_profile(data))
    data["criteria"]["vocabulary_packs"] = "ai-ml"
    assert any("vocabulary_packs" in error for error in profiles.validate_profile(data))
    data["criteria"]["vocabulary_packs"] = ["../profiles/ai-ml-remote-eu"]
    assert any("vocabulary_packs" in error for error in profiles.validate_profile(data))


def test_bundled_example_matches_fixture():
    bundled = Path(profiles.home.data_dir()) / "profiles" / "ai-ml-remote-eu.yaml"
    assert yaml.safe_load(bundled.read_text(encoding="utf-8")) == _profile("ai-ml-remote-eu")
    assert _profile("ai-ml-remote-eu")["criteria"]["vocabulary_packs"] == ["ai-ml"]


def _job(title: str, body: str) -> ExtractedJob:
    return ExtractedJob(
        title=title,
        company="Example",
        location="Remote",
        remote_text="Fully remote within the EU",
        body_text=body,
        apply_url="https://boards.greenhouse.io/example/jobs/2",
        domain="boards.greenhouse.io",
        apply_available=True,
        is_open_hint=True,
    )


def test_pack_family_is_default_when_profile_has_only_titles():
    profile = _profile("ai-ml-remote-eu")
    profile["criteria"].pop("role_focus_terms")
    result = characterize_job(_job("Senior NLP Engineer", "Build language tooling."), profile, load_allowed_domains())
    assert result["heuristic_evaluator"]["is_allowed_family"] is True


def test_coarse_signal_comes_from_the_pack():
    profile = _profile("ai-ml-remote-eu")
    assert _passes(profile, "Senior Data Science Engineer") is True
    profile["criteria"].pop("vocabulary_packs")
    assert _passes(profile, "Senior Data Science Engineer") is False


def test_non_pack_profile_matches_target_titles_alongside_focus_terms():
    profile = _profile("finance-eu")
    profile["criteria"]["role_focus_terms"] = ["finance manager"]
    profile["criteria"]["target_titles"] = ["Senior Financial Analyst"]
    assert _passes(profile, "Senior Financial Analyst") is True
    assert _passes(profile, "Finance Manager") is True
    job = _job("Senior Financial Analyst", "Own financial modeling and IFRS close. Applications open.")
    result = characterize_job(job, profile, load_allowed_domains())
    assert result["pre_filter"]["keep"] is True
    assert result["heuristic_evaluator"]["is_allowed_family"] is True


def test_coarse_prefilter_handles_accented_exclusions_and_punctuation_signals():
    profile = {
        "criteria": {
            "role_focus_terms": ["backend developer"],
            "role_signal_terms": ["c++"],
            "seniority": {"required": False, "excluded_terms": ["prácticas"]},
        }
    }
    assert _passes(profile, "Becario en Practicas Backend Developer") is False
    assert _passes(profile, "Platform Systems Programmer C++") is True


@pytest.mark.parametrize("title", [
    "Materials and Processes Engineer",
    "Ingeniero de Materiales y Procesos",
    "Ingeniero de Proceso - Zaragoza",
    "Ingeniera de Proceso - Zaragoza",
    "Ingeniero/a de Proceso - Zaragoza",
    "Ing. de Proceso - Zaragoza",
    "Ing. de Procesos - Zaragoza",
    "Ingeniero/a de Fabricación - Ortuella",
    "Ingeniería de procesos / Process Engineering",
])
def test_industrial_title_aliases_match_in_discovery_and_filter(title):
    profile = {
        "criteria": {
            "role_focus_terms": ["materials engineer", "process engineer", "manufacturing engineer"],
            "seniority": {"required": False},
        },
    }
    assert _passes(profile, title) is True
    result = characterize_job(_job(title, ""), profile, load_allowed_domains())
    assert result["heuristic_evaluator"]["is_allowed_family"] is True
    assert _passes(profile, "Manufacturing Operative") is False


def test_optional_seniority_admits_graduate_unless_profile_excludes_it():
    profile = {"criteria": {"role_focus_terms": ["process engineer"], "seniority": {"required": False}}}
    assert _passes(profile, "Graduate Process Engineer") is True
    profile["criteria"]["seniority"]["excluded_terms"] = ["graduate"]
    assert _passes(profile, "Graduate Process Engineer") is False
    profile["criteria"]["seniority"] = {"required": True}
    assert _passes(profile, "Graduate Process Engineer") is False


@pytest.mark.parametrize("separator", [" - ", " \u2013 ", " \u2014 "])
def test_graduate_role_matches_with_a_dash_separator(separator):
    profile = {
        "criteria": {
            "role_focus_terms": ["occupational therapist graduate program"],
            "seniority": {"required": False},
        },
    }
    title = f"Occupational Therapist{separator}Graduate Program 2027"
    assert _passes(profile, title) is True
    result = characterize_job(_job(title, "Occupational therapy graduate program."), profile, load_allowed_domains())
    assert result["heuristic_evaluator"]["is_allowed_family"] is True


def test_dashed_title_matches_an_active_pack():
    title = "Occupational Therapist - Graduate Program"
    pack = VocabularyPack(role_terms=("occupational therapist graduate program",))
    score, signals = score_role_family(title, "", vocabulary=pack)
    assert score == score_role_family(title.replace(" - ", " "), "", vocabulary=pack)[0]
    assert any(signal.startswith("title:occupational therapist") for signal in signals)


def test_dashed_synonym_group_uses_normalized_members():
    term = "Occupational Therapist - Graduate Program"
    assert expand_role_terms([term], extra_groups=[(term, "Graduate Occupational Therapist")]) == [
        "occupational therapist graduate program", "graduate occupational therapist",
    ]


def test_dashed_focus_terms_match_in_the_posting_body():
    terms = ("Occupational Therapist - Graduate Program", "Patient Care - Rehabilitation")
    body = "Join our Occupational Therapist - Graduate Program. Provide patient care - rehabilitation."
    assert is_allowed_family("Occupational Therapist", body, allowed_terms=terms)


@pytest.mark.parametrize(("title", "body", "discard_reason"), [
    ("Graduate Process Engineer", "Optimize production.", "seniority_not_senior"),
    (
        "Manufacturing Engineer Intership",
        "Employment Type:\nInternship\nExperience Level:\nStudent",
        "seniority_not_senior",
    ),
    ("Manufacturing Engineer", "Mentor students in our graduate programme.", None),
])
def test_optional_seniority_uses_explicit_level_exclusions(title, body, discard_reason):
    profile = {"criteria": {
        "role_focus_terms": ["process engineer", "manufacturing engineer"],
        "seniority": {"required": False, "excluded_terms": ["internship", "student", "graduate"]},
    }}
    result = characterize_job(_job(title, body), profile, load_allowed_domains())
    assert result["pipeline_characterization"]["reason"] == discard_reason
