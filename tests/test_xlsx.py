from __future__ import annotations

import json
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook

from ultron import profiles, runs, xlsx
from ultron.cli import main


def _row(title="Engineer", url="https://example.com/job/1"):
    return {
        "title": title, "company": "Acme", "apply_url": url, "location": "Madrid",
        "remote_policy": "remote", "eligibility": "eligible", "eligibility_evidence": "ok",
        "fit_signals": ["python", "ml"], "freshness": "new", "evaluation_verdict": "keep",
        "match_score": 80, "is_new": True,
    }


@pytest.fixture
def run_id():
    profiles.install_example()
    rid = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    d = runs.run_dir(rid)
    results = {
        "run_id": rid,
        "rows": [_row(), _row("=HYPERLINK(\"http://evil\",\"x\")", "https://example.com/job/2")],
        "summary": {"run_id": rid, "roles_kept": 2, "ats_pages_opened": 3, "discard_counts": {"stale": 2}},
        "discards": [{"reason": "dup", "apply_url": "https://x.test/9"}],
    }
    (d / "results.json").write_text(json.dumps(results), encoding="utf-8")
    runs.write_jsonl(d / "discarded.jsonl", [
        {"apply_url": "https://example.com/a", "reason": "stale", "title": "Old", "company": "Z",
         "identity_key": "k1", "stage": "filter"},
    ])
    runs.write_jsonl(d / "finalize_discards.jsonl", [
        {"apply_url": "https://example.com/b", "reason": "final_cap", "title": "Cap", "company": "Y",
         "identity_key": "k2", "stage": "final_cap"},
    ])
    return rid


def test_workbook_sheets(run_id):
    path = xlsx.export_run_to_excel(run_id)
    assert path == runs.run_dir(run_id) / "results.xlsx"
    wb = load_workbook(path)
    assert wb.sheetnames == ["Summary", "Roles (2)", "Discards (2)"]
    assert wb["Roles (2)"].max_row == 3
    assert wb["Roles (2)"].auto_filter.ref
    assert wb["Roles (2)"].freeze_panes == "A2"
    assert wb["Roles (2)"]["Q2"].hyperlink is not None
    d = wb["Discards (2)"]
    assert [d.cell(row=r, column=1).value for r in (2, 3)] == ["stale", "final_cap"]


def test_repeated_cli_exports_preserve_workbook_content_and_format(run_id, capsys, tmp_path):
    outputs = [tmp_path / "first.xlsx", tmp_path / "second.xlsx"]
    for output in outputs:
        assert main(["export", "xlsx", "--run", run_id, "--out", str(output)]) == 0
        assert json.loads(capsys.readouterr().out)["path"] == str(output)

    with ZipFile(outputs[0]) as first, ZipFile(outputs[1]) as second:
        assert set(first.namelist()) == set(second.namelist())
        for part in first.namelist():
            if part != "docProps/core.xml":  # The workbook timestamps can change.
                assert first.read(part) == second.read(part), part


def test_formula_neutralized(run_id):
    path = xlsx.export_run_to_excel(run_id, runs.run_dir(run_id) / "out.xlsx")
    ws = load_workbook(path)["Roles (2)"]
    cell = ws["B3"]
    assert cell.data_type == "s"
    assert cell.value.startswith("'=HYPERLINK")


def test_missing_openpyxl(run_id, monkeypatch):
    import builtins

    real = builtins.__import__

    def fake(name, *a, **k):
        if name.startswith("openpyxl"):
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake)
    with pytest.raises(xlsx.XlsxUnavailable, match="openpyxl not installed: reinstall ULTRON with the xlsx extra"):
        xlsx.export_run_to_excel(run_id)


def test_cli_export_and_unknown_run(run_id, capsys, tmp_path):
    out = tmp_path / "x.xlsx"
    assert main(["export", "xlsx", "--run", run_id, "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["path"] == str(out)
    assert out.is_file()
    assert main(["export", "xlsx", "--run", "nope"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "run_not_found"


def test_numbers_native_and_unsafe_urls_not_linked(run_id):
    d = runs.run_dir(run_id)
    res = json.loads((d / "results.json").read_text(encoding="utf-8"))
    res["rows"] = [{**_row(url="javascript:alert(1)"), "match_score": -5},
                   {**_row(url="file:///etc/passwd")}]
    (d / "results.json").write_text(json.dumps(res), encoding="utf-8")
    ws = load_workbook(xlsx.export_run_to_excel(run_id))["Roles (2)"]
    assert ws["L2"].value == -5 and ws["L2"].data_type == "n"
    assert ws["L3"].value == 80
    assert ws["R2"].value is True
    assert ws["Q2"].hyperlink is None and ws["Q3"].hyperlink is None


def test_empty_rows_export(run_id):
    d = runs.run_dir(run_id)
    (d / "results.json").write_text(json.dumps({"run_id": run_id, "rows": [], "summary": {}}), encoding="utf-8")
    (d / "discarded.jsonl").unlink()
    (d / "finalize_discards.jsonl").unlink()
    wb = load_workbook(xlsx.export_run_to_excel(run_id))
    assert wb.sheetnames == ["Summary", "Roles (0)", "Discards (0)"]


def test_missing_results_json_is_export_failed(run_id, capsys):
    (runs.run_dir(run_id) / "results.json").unlink()
    assert main(["export", "xlsx", "--run", run_id]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "export_failed"


def test_control_characters_are_stripped(run_id, capsys, tmp_path):
    d = runs.run_dir(run_id)
    results = json.loads((d / "results.json").read_text(encoding="utf-8"))
    results["rows"][0]["title"] = "Back\x08end \x1b[1mEngineer\x0b"
    results["summary"]["discard_counts"] = {"sta\x1ble": 2, "dup": "1\x08"}
    (d / "results.json").write_text(json.dumps(results), encoding="utf-8")
    runs.write_jsonl(d / "discarded.jsonl", [
        {"apply_url": "https://example.com/a", "reason": "stale", "title": "Old\x08", "company": "Z",
         "detail": "posted \x1b[0m\x0b=1+1", "stage": "filter"},
    ])
    out = tmp_path / "ctrl.xlsx"
    assert main(["export", "xlsx", "--run", run_id, "--out", str(out)]) == 0
    assert json.loads(capsys.readouterr().out)["path"] == str(out)
    wb = load_workbook(out)
    assert wb["Roles (2)"]["B2"].value == "Backend [1mEngineer"
    assert wb["Summary"]["A17"].value == "stale"
    assert (wb["Summary"]["A18"].value, wb["Summary"]["B18"].value) == ("dup", "1")
    discards = wb["Discards (2)"]
    assert discards["D2"].value == "Old"
    assert discards["G2"].value == "posted [0m=1+1"


def test_control_characters_cannot_unmask_a_formula():
    assert xlsx._norm("\x08=1+1") == "'=1+1"
    assert xlsx._norm(["\x1b@cmd", "x"]) == "'@cmd | x"
