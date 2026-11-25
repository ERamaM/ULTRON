"""Tests for shared export presentation rules."""

import pytest

from ultron.exports import view_model


@pytest.mark.parametrize(("rows", "mode"), [
    ([], "scored"),
    ([{"match_score": 0}, {"match_band": "good"}], "scored"),
    ([{"priority": 1}, {"found_by": "agent_verified"}], "toolkit"),
    ([{"match_band": "good"}, {"priority": 3}], "mixed"),
    ([{"priority": 1, "match_score": 0}], "scored"),
])
def test_export_mode(rows, mode):
    assert view_model.detect_export_mode(rows) == mode


@pytest.mark.parametrize(("raw", "expected"), [(1, 1), ("2", 2), (3, 3), (True, None), (1.0, None), (4, None)])
def test_priority(raw, expected):
    assert view_model.normalize_priority(raw) == expected
    assert view_model.priority_label(raw).startswith(f"P{expected}") if expected else view_model.priority_label(raw) == ""


def test_vacancy_date_precedence():
    assert view_model.best_vacancy_date({
        "published_at": "2026-02-30",
        "updated_at": "2026-02-28T12:15:10Z",
        "first_seen": "2026-02-12",
    }) == {"iso": "2026-02-28", "kind": "updated", "label": "Updated 2026-02-28"}
    assert view_model.best_vacancy_date({"freshness": "2026-02-12"})["kind"] == "unverified"
    assert view_model.best_vacancy_date({"published_at": "2026-02-28 trailing"}) is None


def test_monogram():
    assert view_model.company_monogram("Acme Corp") == view_model.company_monogram("acme corp")
    assert view_model.company_monogram("Acme Corp")["initials"] == "AC"
    assert view_model.company_monogram("n8n")["initials"] == "N8"
    assert view_model.company_monogram("")["initials"] == "?"
    assert 0 <= view_model.company_monogram("Acme Corp")["tone"] < 8


def test_tags_and_remote_labels():
    assert view_model.split_fit_signals("Python|python, AWS,\x00 Rust", max_tags=2) == ["Python", "AWS"]
    assert view_model.split_fit_signals([5, "a" * 80]) == ["a" * 60]
    assert view_model.split_fit_signals(None) == []
    assert view_model.remote_label("REMOTE_EU") == "Remote \u00b7 EU"
    assert view_model.remote_label("remote") == ""


@pytest.mark.parametrize(("text", "formatted", "midpoint"), [
    ("", "", None),
    ("Salary up to EUR 120,000", "EUR 120,000 / year", ("EUR", 120_000)),
    ("EUR 80,000 to 110,000 per year", "EUR 80,000\u2013110,000 / year", ("EUR", 95_000)),
    ("GBP 75.50 per hour", "GBP 75.5 / hour", None),
])
def test_salary_disclosures(text, formatted, midpoint):
    assert view_model.format_salary_disclosure(text) == formatted
    assert view_model.parse_salary_midpoint(formatted) == midpoint


def test_salary_summary_keeps_currencies_separate():
    result = view_model.summarize_job_display([
        {"salary": "EUR 50,000 / year"}, {"salary": "EUR 70,000 / year"},
        {"salary": "GBP 40,000 / year"}, {"salary": "GBP 75 / hour"}, {},
    ])
    assert result["salary_summary"] == {"disclosed_count": 4, "by_currency": {
        "EUR": {"count": 2, "min": 50_000, "median": 60_000, "max": 70_000},
        "GBP": {"count": 1, "min": 40_000, "median": 40_000, "max": 40_000},
    }}
    assert view_model.parse_salary_midpoint("EUR 120,000-90,000 per year") is None
    assert view_model.parse_salary_midpoint("EUR 95,000 / year or USD 90,000 / year") is None
