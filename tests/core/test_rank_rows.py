from __future__ import annotations

from ultron.core.models import FinalRow
from ultron.core.ranker.final_rows import rank_final_rows


def _row(**kw):
    base = {
        "title": "Senior ML Engineer",
        "company": "Acme",
        "apply_url": "https://boards.greenhouse.io/acme/jobs/1",
        "location": "Remote - EU",
        "remote_policy": "remote_eu",
        "eligibility": "high",
        "eligibility_evidence": "",
        "fit_signals": ["LLM"],
        "freshness": "recent",
    }
    base.update(kw)
    return FinalRow(**base)


def test_keep_ranks_before_unsure():
    rows = [
        _row(evaluation_verdict="unsure", apply_url="https://x.com/1"),
        _row(evaluation_verdict="keep", apply_url="https://x.com/2"),
    ]
    ranked = rank_final_rows(rows)
    assert ranked[0].evaluation_verdict == "keep"


def test_eligibility_orders_within_same_verdict():
    rows = [
        _row(eligibility="unknown", remote_policy="unknown", apply_url="https://x.com/1"),
        _row(eligibility="confirmed", remote_policy="remote_spain", apply_url="https://x.com/2"),
        _row(eligibility="medium", remote_policy="remote_emea", apply_url="https://x.com/3"),
    ]
    ranked = rank_final_rows(rows)
    assert [r.eligibility for r in ranked] == ["confirmed", "medium", "unknown"]


def test_sort_is_stable_within_tier():
    rows = [
        _row(company="First", apply_url="https://x.com/1"),
        _row(company="Second", apply_url="https://x.com/2"),
    ]
    ranked = rank_final_rows(rows)
    assert [r.company for r in ranked] == ["First", "Second"]


def test_score_ranking_is_reproducible_with_documented_tie_break():
    rows = [
        _row(company="Zulu", match_score=90, match_band="strong", apply_url="https://x.com/2"),
        _row(company="Alpha", match_score=90, match_band="strong", apply_url="https://x.com/1"),
        _row(company="Best", match_score=96, match_band="strong", apply_url="https://x.com/3"),
    ]
    expected = ["Best", "Alpha", "Zulu"]
    assert [row.company for row in rank_final_rows(rows, use_match_score=True)] == expected
    assert [row.company for row in rank_final_rows(list(reversed(rows)), use_match_score=True)] == expected


def test_shadow_mode_keeps_legacy_verdict_before_score():
    rows = [
        _row(evaluation_verdict="unsure", match_score=99, match_band="strong", apply_url="https://x.com/1"),
        _row(evaluation_verdict="keep", match_score=60, match_band="borderline", apply_url="https://x.com/2"),
    ]
    assert rank_final_rows(rows)[0].evaluation_verdict == "keep"
    assert rank_final_rows(rows, use_match_score=True)[0].evaluation_verdict == "unsure"


def test_shadow_score_precedes_company_and_unscored_rows():
    rows = [
        _row(company="Aardvark", match_score=None),
        _row(company="Alpha", match_score=60),
        _row(company="Zulu", match_score=90),
        _row(company="Zero", match_score=0),
    ]
    assert [row.company for row in rank_final_rows(rows)] == ["Zulu", "Alpha", "Zero", "Aardvark"]
