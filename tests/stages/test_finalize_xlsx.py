from __future__ import annotations

import builtins
import json

from openpyxl import load_workbook
from stage_helpers import make_run

from ultron.cli import main
from ultron.storage import runs

CASES = ["en-positive-shape-01", "en-positive-shape-02", "en-positive-shape-03", "en-ambiguous-shape-01"]


def _filtered(capsys):
    run_id = make_run(CASES)
    assert main(["filter", "--run", run_id]) == 0
    capsys.readouterr()
    return run_id


def test_finalize_xlsx_real_path(capsys):
    run_id = _filtered(capsys)
    assert main(["finalize", "--run", run_id, "--xlsx"]) == 0
    out = json.loads(capsys.readouterr().out)
    d = runs.run_dir(run_id)
    assert out["outputs"]["results_xlsx"] == str(d / "results.xlsx")
    results = json.loads((d / "results.json").read_text(encoding="utf-8"))
    n_disc = len(runs.read_jsonl(d / "discarded.jsonl")) + len(runs.read_jsonl(d / "finalize_discards.jsonl"))
    wb = load_workbook(d / "results.xlsx")
    companies = {row["company"].casefold() for row in results["rows"]}
    assert wb.sheetnames == [
        "Summary", f"Roles ({len(results['rows'])})", f"Companies ({len(companies)})", f"Discards ({n_disc})",
        "Vacancy Dates",
    ]
    assert wb["Vacancy Dates"]["E2"].value == results["rows"][0]["first_seen"]
    assert len(results["rows"]) >= 1
    assert wb[wb.sheetnames[1]].max_row == len(results["rows"]) + 1


def test_finalize_xlsx_missing_openpyxl_keeps_payload(capsys, monkeypatch):
    run_id = _filtered(capsys)
    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("openpyxl"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    assert main(["finalize", "--run", run_id, "--xlsx"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "openpyxl not installed" in out["xlsx_error"]
    assert out["roles"] >= 1 and "results_xlsx" not in out["outputs"]
    assert (runs.run_dir(run_id) / "results.json").is_file()


def test_finalize_xlsx_with_control_characters_in_discards(capsys):
    run_id = _filtered(capsys)
    path = runs.run_dir(run_id) / "discarded.jsonl"
    rows = runs.read_jsonl(path)
    rows.append({"apply_url": "https://example.com/x", "reason": "stale", "title": "Bad\x08 title\x1b",
                 "detail": "line\x0bbreak", "stage": "filter", "identity_key": "ctrl"})
    runs.write_jsonl(path, rows)
    assert main(["finalize", "--run", run_id, "--xlsx"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "xlsx_error" not in out
    assert out["outputs"]["results_xlsx"] == str(runs.run_dir(run_id) / "results.xlsx")
