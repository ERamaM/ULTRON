from __future__ import annotations

import json

from stage_helpers import make_run

from ultron.cli import main
from ultron.storage import runs

CASES = ["en-positive-shape-01", "en-positive-shape-02", "en-ambiguous-shape-01"]


def test_finalize_html_writes_report(capsys):
    run_id = make_run(CASES)
    assert main(["filter", "--run", run_id]) == 0
    capsys.readouterr()
    assert main(["finalize", "--run", run_id, "--html"]) == 0
    out = json.loads(capsys.readouterr().out)
    path = runs.run_dir(run_id) / "results.html"
    assert out["outputs"]["results_html"] == str(path)
    assert "html_error" not in out
    page = path.read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>") and "Content-Security-Policy" in page


def test_finalize_without_html_flag_writes_no_report(capsys):
    run_id = make_run(CASES)
    assert main(["filter", "--run", run_id]) == 0
    capsys.readouterr()
    assert main(["finalize", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "results_html" not in out["outputs"]
    assert not (runs.run_dir(run_id) / "results.html").exists()
