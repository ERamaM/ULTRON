"""Gate scoring must recognize terms, not fragments of unrelated words."""

import json
from pathlib import Path

import pytest

from ultron.core.decision.ambiguity import AmbiguityScorer
from ultron.core.models import ExtractedJob, JobEvaluation
from ultron.core.vocabulary import load_packs

AI = load_packs(["ai-ml"])


@pytest.mark.parametrize(
    "text,expected",
    [
        ("facet storage leverage average", 0),
        ("CET", 10),
        ("EMEA", 10),
        ("time zone", 10),
        ("remote hybrid", 16),
        ("remotely hybrid", 0),
        ("rag", 15),
        ("llm", 15),
        ("machine learning", 15),
    ],
)
def test_terms_have_word_boundaries(text, expected):
    assert AmbiguityScorer._text_signal_score("Engineer\n" + text, "role_family", AI) == expected


def test_role_text_signals_need_a_pack():
    assert AmbiguityScorer._text_signal_score("Engineer\nllm", "role_family") == 0
    assert AmbiguityScorer(vocabulary=AI).vocabulary is AI


@pytest.mark.parametrize("case_id", ["q-cet-substring", "q-leuven-region-name"])
def test_corpus_score_changes_only_for_substring_bug(case_id):
    corpus = json.loads(
        (Path(__file__).parent.parent / "fixtures/heuristic_characterization_corpus.json").read_text(encoding="utf-8")
    )
    case = next(case for case in corpus["cases"] if case["id"] == case_id)
    job = ExtractedJob(**{**corpus["defaults"], **case["job"], "apply_url": "https://example.com/job"})
    fallback = JobEvaluation(
        keep=False,
        verdict="discard",
        discard_category="remote_policy",
        is_senior=True,
        is_allowed_family=True,
        is_full_remote=False,
        eligibility="high",
    )
    score = AmbiguityScorer().score(job, fallback)
    assert score == 62
    legacy_substring_increment = 10 if case_id == "q-cet-substring" else 0
    assert ("cet" in job.body_text.lower()) == bool(legacy_substring_increment)
    # Keep scores stay fixed even when the same corpus body contains "facet".
    assert AmbiguityScorer().score(job, fallback.model_copy(update={"keep": True, "verdict": "keep"})) == 85
