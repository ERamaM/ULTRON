"""Parity with MARK III's 60-case heuristic characterization golden.

The golden is a behaviour snapshot produced by MARK III; it must never be edited.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.models import ExtractedJob

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS = json.loads((FIXTURES / "heuristic_characterization_corpus.json").read_text(encoding="utf-8"))
GOLDEN = json.loads((FIXTURES / "heuristic_characterization_golden.json").read_text(encoding="utf-8"))
PROFILE = yaml.safe_load((FIXTURES / "profiles" / "ai-ml-remote-eu.yaml").read_text(encoding="utf-8"))
GOLDEN_BY_ID = {case["id"]: case for case in GOLDEN["cases"]}
COMPARED_KEYS = ("classification_signals", "pre_filter", "heuristic_evaluator", "hard_filter", "pipeline_characterization")
# Reviewed fixes where ULTRON intentionally differs from the MARK III snapshot. The golden file stays untouched;
# each entry overrides only the named fields and says why.
INTENTIONAL_DEVIATIONS = {
    # Specialties no longer match in the middle of a word: "eval" inside "retrieval", "RAG" inside "storage".
    "en-positive-shape-02": {"heuristic_evaluator": {"fit_signals": ["LLM", "retrieval"]}},
    "es-positive-shape-02": {"heuristic_evaluator": {"fit_signals": ["LLM", "retrieval"]}},
    "q-cet-substring": {"heuristic_evaluator": {"fit_signals": []}},
    # Explicit closed-status phrases ("the previous research role has been filled"; ES "la oferta esta cerrada",
    # "la plaza ya ha sido cubierta") now reject the posting as expired_or_closed. Both cases are labelled
    # not_relevant (closed) in the quality labels, and MARK III kept them open only because its phrase list
    # missed these forms.
    **{
        case_id: {
            "pre_filter": {"keep": False, "reason": "expired_or_closed"},
            "heuristic_evaluator": {
                "discard_category": "closed_or_invalid", "discard_reason": "expired_or_closed", "keep": False,
                "open_status_ok": False, "verdict": "discard",
            },
            "hard_filter": {"keep": False, "reason": "expired_or_closed"},
            "pipeline_characterization": {"outcome": "discard", "reason": "expired_or_closed", "stage": "pre_filter"},
        }
        for case_id in ("en-negative-shape-08", "es-negative-shape-07")
    },
    # "No geographic restriction or office attendance" negates office attendance too; MARK III read it as an
    # onsite conflict and discarded a fully remote worldwide role that the quality labels mark relevant.
    "en-positive-shape-07": {
        "heuristic_evaluator": {
            "discard_category": None, "discard_reason": None, "is_full_remote": True, "keep": True, "verdict": "keep",
        },
        "hard_filter": {"keep": True, "reason": None},
        "pipeline_characterization": {"outcome": "keep", "reason": None, "stage": "completed"},
    },
}


def _job_from_case(case: dict) -> ExtractedJob:
    payload = {**CORPUS["defaults"], **dict(case["job"])}
    payload.setdefault("apply_url", f"https://boards.greenhouse.io/example/jobs/{case['id']}")
    return ExtractedJob(**payload)


def test_corpus_and_golden_cover_the_same_60_cases():
    corpus_ids = [case["id"] for case in CORPUS["cases"]]
    assert len(corpus_ids) == 60
    assert len(GOLDEN["cases"]) == 60
    assert set(corpus_ids) == set(GOLDEN_BY_ID)


@pytest.mark.parametrize("case", CORPUS["cases"], ids=[case["id"] for case in CORPUS["cases"]])
def test_all_60_cases_match_mark_iii_golden(case):
    result = characterize_job(_job_from_case(case), PROFILE, load_allowed_domains())
    actual = {key: value for key, value in result.items() if not key.startswith("_")}
    expected = {key: GOLDEN_BY_ID[case["id"]][key] for key in COMPARED_KEYS}
    for key, fields in INTENTIONAL_DEVIATIONS.get(case["id"], {}).items():
        expected[key] = {**expected[key], **fields}
    assert set(actual) == set(COMPARED_KEYS), case["id"]
    diff = {key: {"expected": expected[key], "actual": actual[key]} for key in COMPARED_KEYS if actual[key] != expected[key]}
    assert not diff, f"{case['id']}: {json.dumps(diff, ensure_ascii=False, indent=2)}"
    assert result["_evaluation"].verdict == expected["heuristic_evaluator"]["verdict"]
    assert result["_signals"].role_score == expected["classification_signals"]["role_score"]
