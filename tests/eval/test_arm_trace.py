"""Miss trace: the outcome of each relevant job an ULTRON arm did not export."""

from __future__ import annotations

import json

import arm_trace as at
import pytest
from arm_inputs import ComparisonError


def _url(n):
    return f"https://jobs.example.test/{n}"


def _jsonl(rows):
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def _texts():
    identity = at._identity
    return {
        "results.json": json.dumps({"rows": [{"apply_url": _url(1)}, {"apply_url": _url(2)}]}),
        "discarded.jsonl": _jsonl([{"apply_url": _url(3), "stage": "pre_filter", "reason": "non_eu_remote"}]),
        "finalize_discards.jsonl": _jsonl([{"apply_url": _url(4), "stage": "dedup", "reason": "duplicate_role"}]),
        "not_evaluated.jsonl": _jsonl([{"identity_key": identity(_url(5))}]),
        "ambiguous.jsonl": _jsonl([{"identity_key": identity(_url(6)), "job": {"apply_url": _url(6)}},
                                   {"identity_key": identity(_url(7)), "job": {"apply_url": _url(7)}}]),
        "evaluations.jsonl": _jsonl([{"identity_key": identity(_url(6)), "evaluation": {"keep": False}}]),
        "postings.jsonl": _jsonl([{"identity_key": identity(_url(8)), "candidate": {"normalized_url": _url(8)}}]),
        "fetch_errors.jsonl": _jsonl([{"url": _url(9), "error": "inaccessible"}]),
        "candidates.jsonl": _jsonl([{"normalized_url": _url(10), "ats_company": "Agent Only Co"},
                                    {"normalized_url": _url(9), "ats_company": "Other"}]),
        "board_report.jsonl": _jsonl([{"seed_key": "greenhouse:queried", "company": "Queried Co", "listed": 3}]),
    }


@pytest.mark.parametrize("n, company, expected", [
    (1, "", "exported"),
    (2, "", "exported_below_top_n"),
    (3, "", "discarded:pre_filter:non_eu_remote"),
    (4, "", "discarded:dedup:duplicate_role"),
    (5, "", "not_evaluated"),
    (6, "", "evaluated_rejected"),
    (7, "", "not_evaluated"),
    (8, "", "posting_not_queued"),
    (9, "", "fetch_failed"),
    (10, "", "candidate_not_fetched"),
    (11, "Queried Co", "employer_queried_job_missing"),
    (12, "Unknown Co", "employer_never_queried"),
    (13, "Agent Only Co", "employer_never_queried"),  # an agent find is not a board query
])
def test_run_view_outcomes(n, company, expected):
    view = at.RunView(_texts())
    assert view.outcome({at._identity(_url(n))}, company, top_n=1) == expected


def test_trace_arm_counts_missed_relevant_jobs():
    jobs = {f"j{n}": {"title": f"T{n}", "company": "Unknown Co", "variants": [{"apply_url": _url(n), "location": ""}]}
            for n in (1, 3, 12)}
    pooled = {"jobs": jobs, "rows": {("scout", "1", 0): "j1"}}
    labels = {"j1": {"relevant": True}, "j3": {"relevant": True}, "j12": {"relevant": False}}
    result = at.trace_arm({"top_n": 25}, "scout", pooled, labels, {"1": at.RunView(_texts())})
    assert result["relevant_missed"] == 1
    assert result["outcome_counts"] == {"discarded:pre_filter:non_eu_remote": 1}
    assert result["jobs"][0]["outcomes"] == {"1": "discarded:pre_filter:non_eu_remote"}


def test_load_run_view_reports_unreadable_runs(tmp_path):
    with pytest.raises(ComparisonError, match="cannot be traced"):
        at.load_run_view("not-a-run", str(tmp_path))


def test_run_view_falls_back_to_ats_candidates_without_a_board_report():
    texts = {"candidates.jsonl": _jsonl([{"normalized_url": _url(1), "ats_company": "Expanded Co",
                                          "source_type": "ats_api"}])}
    view = at.RunView(texts)
    assert view.outcome({at._identity(_url(2))}, "Expanded Co", top_n=25) == "employer_queried_job_missing"
