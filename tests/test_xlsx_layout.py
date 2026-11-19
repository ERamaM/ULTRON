"""Visual layout of the Excel report: header, panes, filters, conditional cues, summary, print setup."""

from __future__ import annotations

import json

import pytest
from openpyxl import load_workbook

from ultron import profiles, runs, xlsx

OLD_ROLE_HEADERS = [
    "Company", "Title", "Location", "Remote", "Eligibility", "Eligibility Evidence", "Freshness", "Fit Signals",
    "Verdict", "Source Type", "Source", "Match Score", "Match Band", "Matched", "Missing", "Uncertain",
    "Apply URL", "New",
]


def _row(title="Engineer", url="https://example.com/job/1", band="strong", score=82, is_new=True, elig="confirmed"):
    return {
        "title": title, "company": "Acme", "apply_url": url, "location": "Remote",
        "remote_policy": "remote_eu", "eligibility": elig, "eligibility_evidence": "Remote within the EU",
        "fit_signals": ["python", "ml"], "freshness": "2026-11-01", "evaluation_verdict": "keep",
        "match_score": score, "match_band": band, "is_new": is_new,
    }


@pytest.fixture
def run_id():
    profiles.install_example()
    rid = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    d = runs.run_dir(rid)
    rows = [
        _row(),
        _row("=cmd|' /C calc'!A0", "javascript:alert(1)", "good", 64, False, "high"),
        _row("Analyst", "https://example.org/jobs/3", "borderline", 41, True, "medium"),
    ]
    results = {
        "run_id": rid,
        "rows": rows,
        "summary": {"run_id": rid, "roles_kept": 3, "ats_pages_opened": 9, "discard_counts": {"stale": 1}},
    }
    (d / "results.json").write_text(json.dumps(results), encoding="utf-8")
    runs.write_jsonl(d / "discarded.jsonl", [
        {"apply_url": "https://example.com/a", "reason": "stale", "title": "Old", "company": "Z", "stage": "filter"},
    ])
    return rid


def _headers(ws) -> list:
    return [c.value for c in ws[1]]


def _col(ws, header: str) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(_headers(ws).index(header) + 1)


def _rules(ws) -> list[tuple[str, object]]:
    return [(str(cf.sqref), rule) for cf in ws.conditional_formatting for rule in cf.rules]


def _wb(run_id):
    return load_workbook(xlsx.export_run_to_excel(run_id))


def test_existing_columns_kept_in_order_and_apply_link_appended(run_id):
    ws = _wb(run_id)["Roles (3)"]
    assert _headers(ws) == [*OLD_ROLE_HEADERS, "Apply"]
    url_col, apply_col = _col(ws, "Apply URL"), _col(ws, "Apply")
    assert ws[f"{url_col}2"].value == "https://example.com/job/1"
    assert ws[f"{url_col}2"].hyperlink.target == "https://example.com/job/1"
    assert ws[f"{apply_col}2"].value == "Open"
    assert ws[f"{apply_col}2"].hyperlink.target == "https://example.com/job/1"
    assert ws[f"{apply_col}2"].data_type == "s"  # a plain cell hyperlink, never a HYPERLINK() formula
    # Unsafe schemes are neither linked nor offered as "Open".
    assert ws[f"{apply_col}3"].value is None and ws[f"{apply_col}3"].hyperlink is None
    assert ws[f"{url_col}3"].hyperlink is None
    # Formula neutralization is unchanged.
    assert ws["B3"].value.startswith("'=cmd")


def test_header_row_is_styled_and_panes_frozen(run_id):
    wb = _wb(run_id)
    for name in ("Roles (3)", "Discards (1)"):
        ws = wb[name]
        cell = ws["A1"]
        assert cell.font.bold
        assert cell.fill.fill_type == "solid"
        assert cell.alignment.wrap_text
        assert ws.freeze_panes == "B2"  # header row and the first identifying column
        assert ws.auto_filter.ref == f"A1:{ws.cell(row=1, column=ws.max_column).column_letter}{ws.max_row}"


def test_data_rows_wrap_with_consistent_height(run_id):
    ws = _wb(run_id)["Roles (3)"]
    evidence = _col(ws, "Eligibility Evidence")
    assert ws[f"{evidence}2"].alignment.wrap_text
    heights = {ws.row_dimensions[r].height for r in range(2, ws.max_row + 1)}
    assert len(heights) == 1 and heights.pop()


def test_conditional_formatting_cues(run_id):
    ws = _wb(run_id)["Roles (3)"]
    rules = _rules(ws)
    score, band, elig = _col(ws, "Match Score"), _col(ws, "Match Band"), _col(ws, "Eligibility")
    assert any(rule.type == "dataBar" and ref == f"{score}2:{score}4" for ref, rule in rules)
    band_rules = [rule for ref, rule in rules if ref == f"{band}2:{band}4"]
    elig_rules = [rule for ref, rule in rules if ref == f"{elig}2:{elig}4"]
    assert {r.formula[0] for r in band_rules} >= {'"strong"', '"good"', '"borderline"', '"ineligible"'}
    assert {r.formula[0] for r in elig_rules} >= {'"confirmed"', '"high"', '"medium"', '"unknown"'}
    for rule in band_rules + elig_rules:
        assert rule.dxf.fill.fgColor.rgb or rule.dxf.fill.bgColor.rgb
    new_rules = [rule for _ref, rule in rules if rule.type == "expression" and "=TRUE" in rule.formula[0]]
    assert new_rules and new_rules[0].dxf.font.bold
    # Pills outrank the new-row highlight.
    assert max(r.priority for r in band_rules + elig_rules) < min(r.priority for r in new_rules)


def test_no_red_green_pair_for_verdict(run_id):
    ws = _wb(run_id)["Roles (3)"]
    verdict = _col(ws, "Verdict")
    fill = ws[f"{verdict}2"].fill.fgColor.rgb.upper()
    assert fill.endswith(("E6F4EA", "FCE8E6")) is False


def test_summary_sheet_first_with_band_and_new_counts(run_id):
    wb = _wb(run_id)
    assert wb.sheetnames[0] == "Summary"
    ws = wb["Summary"]
    pairs = {}
    for row in ws.iter_rows():
        for idx in range(len(row) - 1):
            if isinstance(row[idx].value, str):
                pairs.setdefault(row[idx].value, row[idx + 1].value)
    assert pairs["New since last run"] == 2
    assert pairs["Strong"] == 1 and pairs["Good"] == 1 and pairs["Borderline"] == 1 and pairs["Ineligible"] == 0
    assert pairs["Roles kept"] == 3
    # Existing layout rows stay where they are.
    assert ws["A4"].value == "Run ID" and ws["A17"].value == "stale"


def test_print_setup(run_id):
    wb = _wb(run_id)
    for name in ("Roles (3)", "Discards (1)"):
        ws = wb[name]
        assert ws.page_setup.orientation == "landscape"
        assert ws.sheet_properties.pageSetUpPr.fitToPage
        assert ws.page_setup.fitToWidth == 1 and ws.page_setup.fitToHeight == 0
        assert ws.print_title_rows.replace("$", "") == "1:1"


def test_empty_workbook_has_no_broken_ranges(run_id):
    d = runs.run_dir(run_id)
    (d / "results.json").write_text(json.dumps({"run_id": run_id, "rows": [], "summary": {}}), encoding="utf-8")
    (d / "discarded.jsonl").unlink()
    wb = _wb(run_id)
    ws = wb["Roles (0)"]
    assert _rules(ws) == []
    assert wb["Summary"]["E5"].value is not None
