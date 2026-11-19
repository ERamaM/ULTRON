from __future__ import annotations

from ultron.core.rules.role_matching import score_role_family
from ultron.core.rules.title_synonyms import expand_role_terms
from ultron.core.vocabulary import load_packs

AI = load_packs(["ai-ml"])


def test_ml_engineer_expands_to_long_form():
    expanded = expand_role_terms(["ml engineer"], extra_groups=AI.title_synonym_groups)
    assert "machine learning engineer" in expanded
    assert "ml engineer" in expanded


def test_family_synonyms_require_the_pack():
    assert expand_role_terms(["ml engineer"]) == ["ml engineer"]


def test_spanish_term_expands_to_english():
    expanded = expand_role_terms(["cientifico de datos"])
    assert "data scientist" in expanded


def test_unknown_terms_pass_through_unchanged():
    assert expand_role_terms(["underwater basket weaver"]) == ["underwater basket weaver"]


def test_no_duplicates_and_lowercase():
    expanded = expand_role_terms(["ML Engineer", "machine learning engineer"], extra_groups=AI.title_synonym_groups)
    assert len(expanded) == len(set(expanded))
    assert all(term == term.lower() for term in expanded)


def test_role_focus_synonyms_are_used_by_score_role_family():
    score, signals = score_role_family(
        "Machine Learning Engineer",
        "",
        allowed_terms=["ml engineer"],
        vocabulary=AI,
    )
    assert score >= 4
    assert any(signal.startswith("title:") for signal in signals)
