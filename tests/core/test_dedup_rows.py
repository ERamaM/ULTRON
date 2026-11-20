from __future__ import annotations

import pytest

from ultron.core.models import FinalRow
from ultron.core.ranker import dedupe_final_rows, normalize_title_for_dedup
from ultron.core.ranker.final_rows import RoleIdentityIndex


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


def test_normalize_title_strips_noise():
    assert normalize_title_for_dedup("Senior ML Engineer (Remote)") == "senior ml engineer"
    assert normalize_title_for_dedup("Senior  ML   Engineer") == "senior ml engineer"


def test_same_url_always_dedupes_even_without_company():
    rows = [
        _row(company="company_not_found", apply_url="https://x.com/j/1"),
        _row(company="company_not_found", apply_url="https://x.com/j/1"),
    ]
    kept, dupes = dedupe_final_rows(rows)
    assert len(kept) == 1
    assert len(dupes) == 1


def test_title_variant_dedupes():
    rows = [
        _row(title="Senior ML Engineer", apply_url="https://x.com/j/1"),
        _row(title="Senior ML Engineer (Remote)", apply_url="https://x.com/j/2"),
    ]
    kept, dupes = dedupe_final_rows(rows)
    assert len(kept) == 1
    assert len(dupes) == 1


def test_distinct_locations_are_kept():
    rows = [
        _row(location="Madrid, Spain", apply_url="https://x.com/j/1"),
        _row(location="Berlin, Germany", apply_url="https://x.com/j/2"),
    ]
    kept, dupes = dedupe_final_rows(rows)
    assert len(kept) == 2
    assert dupes == []


def test_same_location_dedupes():
    rows = [
        _row(apply_url="https://x.com/j/1"),
        _row(apply_url="https://x.com/j/2"),
    ]
    kept, dupes = dedupe_final_rows(rows)
    assert len(kept) == 1
    assert len(dupes) == 1


@pytest.mark.parametrize(
    "first_changes,better_changes",
    [
        ({"evaluation_verdict": "unsure"}, {"evaluation_verdict": "keep"}),
        ({"eligibility": "unknown"}, {"eligibility": "high"}),
        ({"match_score": None}, {"match_score": 0}),
        ({"match_score": 60}, {"match_score": 90}),
        ({"location": "location_not_shown"}, {"location": "Madrid"}),
    ],
)
def test_best_duplicate_replaces_in_original_position(first_changes, better_changes):
    first = _row(**first_changes)
    other = _row(company="Other", apply_url="https://example.test/other")
    better = _row(**better_changes)
    assert dedupe_final_rows([first, other, better]) == ([better, other], [first])


def test_equal_duplicate_quality_preserves_first_arrival():
    first = _row(apply_url="https://example.test/1")
    later = _row(apply_url="https://example.test/2")
    assert dedupe_final_rows([first, later]) == ([first], [later])


def test_identity_count_matches_dedup_at_each_prefix():
    rows = [
        _row(location="Madrid", apply_url="https://example.test/1", evaluation_verdict="unsure"),
        _row(location="Berlin", apply_url="https://example.test/2"),
        _row(location="location_not_shown", apply_url="https://example.test/3"),
        _row(location="Madrid", apply_url="https://example.test/4"),
        _row(company="company_not_found", apply_url="https://example.test/1"),
        _row(company="company_not_found", apply_url=""),
        _row(company="company_not_found", apply_url=""),
    ]
    index = RoleIdentityIndex()
    for position, row in enumerate(rows, 1):
        index.add(row)
        assert index.unique_count == len(dedupe_final_rows(rows[:position])[0])
    assert index.unique_count == 4


@pytest.mark.parametrize(
    "title",
    [
        "Senior ML Engineer (Remote - EU)",
        "Senior ML Engineer [Hybrid]",
        "Senior ML Engineer (m/f/d)",
        "Senior ML Engineer (Remote, Spain)",
        "Senior ML Engineer (Remoto / Madrid)",
    ],
)
def test_normalize_title_strips_work_mode_and_location_parentheticals(title):
    assert normalize_title_for_dedup(title) == "senior ml engineer"


def test_normalize_title_keeps_specialty_parenthetical():
    assert normalize_title_for_dedup("Senior Engineer (Payments)") == "senior engineer payments"
    assert normalize_title_for_dedup("Senior Engineer (Payments, Remote)") == "senior engineer payments"


def test_distinct_specialty_parentheticals_are_kept():
    rows = [
        _row(title="Senior Engineer (Payments)", apply_url="https://x.com/j/1"),
        _row(title="Senior Engineer (Search)", apply_url="https://x.com/j/2"),
    ]
    kept, dupes = dedupe_final_rows(rows)
    assert len(kept) == 2
    assert dupes == []


def test_non_ascii_specialty_parentheticals_are_kept():
    assert normalize_title_for_dedup("Engineer (決済)") == "engineer 決済"
    assert normalize_title_for_dedup("Engineer (検索)") == "engineer 検索"
    assert normalize_title_for_dedup("Engineer (Remote, 決済)") == "engineer 決済"
    assert normalize_title_for_dedup("Engineer (Remote)") == "engineer"
    rows = [
        _row(title="Engineer (決済)", apply_url="https://x.com/j/1"),
        _row(title="Engineer (検索)", apply_url="https://x.com/j/2"),
    ]
    kept, dupes = dedupe_final_rows(rows)
    assert len(kept) == 2
    assert dupes == []


def test_location_variants_of_one_remote_posting_merge_into_one_row():
    rows = [
        _row(location="Bogota, Colombia", apply_url="https://x.com/j/1", source_key="greenhouse:acme"),
        _row(location="Lima, Peru", apply_url="https://x.com/j/2", source_key="greenhouse:acme"),
        _row(title="Senior Data Analyst", location="Lima, Peru", apply_url="https://x.com/j/3",
             source_key="greenhouse:acme"),
        _row(location="Quito, Ecuador", apply_url="https://x.com/j/4", source_key="greenhouse:other-board"),
    ]
    kept, dupes = dedupe_final_rows(rows, merge_locations=True)
    assert [row.apply_url for row in kept] == ["https://x.com/j/1", "https://x.com/j/3", "https://x.com/j/4"]
    assert kept[0].location == "Bogota, Colombia; Lima, Peru"
    assert [row.apply_url for row in dupes] == ["https://x.com/j/2"]


def test_location_variants_stay_apart_without_merging():
    rows = [
        _row(location="Dublin, Ireland", apply_url="https://x.com/j/1", source_key="greenhouse:hospital"),
        _row(location="Cork, Ireland", apply_url="https://x.com/j/2", source_key="greenhouse:hospital"),
    ]
    kept, dupes = dedupe_final_rows(rows)
    assert len(kept) == 2 and dupes == []

def test_the_merged_row_is_the_same_whatever_order_the_cities_arrive_in():
    def rows():
        return [
            _row(location="Lima, Peru", apply_url="https://x.com/j/2", source_key="greenhouse:acme"),
            _row(location="Bogota, Colombia", apply_url="https://x.com/j/1", source_key="greenhouse:acme"),
        ]
    forward = dedupe_final_rows(rows(), merge_locations=True)[0]
    backward = dedupe_final_rows(rows()[::-1], merge_locations=True)[0]
    assert [row.apply_url for row in forward] == [row.apply_url for row in backward] == ["https://x.com/j/1"]

def test_web_results_on_different_domains_never_merge():
    rows = [
        _row(location="Madrid, Spain", apply_url="https://careers.acme.example/jobs/1", source_type="web_search",
             source_key="web_search"),
        _row(location="Lisbon, Portugal", apply_url="https://acme-jobs.attacker.example/a", source_type="web_search",
             source_key="web_search"),
    ]
    kept, dupes = dedupe_final_rows(rows, merge_locations=True)
    assert [row.apply_url for row in kept] == [row.apply_url for row in rows] and dupes == []