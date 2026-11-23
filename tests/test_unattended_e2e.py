from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ultron import seeds
from ultron.cli import main
from ultron.core.models import AtsBoardSeed
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.storage import runs

FIX = Path(__file__).resolve().parent / "fixtures"
PAGE = (FIX / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)


def _handler(request: httpx.Request) -> httpx.Response:
    if "greenhouse" in request.url.host and request.url.path.startswith("/v1/boards/acme"):
        return httpx.Response(200, content=(FIX / "ats" / "greenhouse_board.json").read_bytes())
    if request.url.host == "boards.greenhouse.io":
        return httpx.Response(200, text=PAGE)
    return httpx.Response(404)


def test_unattended_e2e(capsys):
    profiles.install_example()
    seeds.add_seeds(
        [AtsBoardSeed(seed_id="greenhouse:acme", company="Acme", ats="greenhouse", board_identifier="acme",
                      priority=1, profiles=[profiles.EXAMPLE_SLUG])],
        profile_slug=profiles.EXAMPLE_SLUG,
    )
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(_handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    code = main(["run", "--unattended", "--profile", profiles.EXAMPLE_SLUG])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    md = Path(out["outputs"]["results_md"])
    assert md.is_file()
    assert out["roles"] >= 1
    assert out["coverage"]["boards"]["completed"] == 1
    assert out["coverage"] == runs.load_run(out["run_id"])["coverage"]
    rows = json.loads(Path(out["outputs"]["results_json"]).read_text(encoding="utf-8"))["rows"]
    assert any(r["evaluation_verdict"] == "keep" for r in rows)
    assert out["agent_evaluated"] == 0 and out["heuristic_fallback"] >= 1
    assert runs.load_run(out["run_id"])["stages_done"][-1] == "finalize"
    assert len([line for line in md.read_text(encoding="utf-8").splitlines() if line.startswith("|")]) >= 3


def test_run_new_still_works(capsys):
    profiles.install_example()
    assert main(["run", "new", "--profile", profiles.EXAMPLE_SLUG]) == 0
    assert "run_id" in json.loads(capsys.readouterr().out)


def test_unattended_coverage_distinguishes_matching_from_reach():
    from ultron.stages.unattended import summarize_coverage

    complete = {"seeds_completed": 4, "seeds_partial": 0, "seeds_failed": 0, "seeds_skipped": 0, "candidates": 8}
    failed = {**complete, "seeds_completed": 1, "seeds_failed": 2, "seeds_skipped": 1}
    stages = {"discover": complete, "fetch": {"fetched": 8}, "filter": {"ambiguous": 2}}

    summary = summarize_coverage(stages, 3, [])
    assert summary["status"] == "completed"
    assert summary["boards"] == {"attempted": 4, "completed": 4, "partial": 0, "failed": 0, "skipped": 0}
    assert summary["pending_ambiguous"] == 2

    limited = summarize_coverage(
        {**stages, "discover": failed}, 3, [{"status": "skipped", "reason": "vendor_budget_exhausted"}],
    )
    assert limited["status"] == "limited" and limited["roles"] == 3
    assert limited["boards"]["attempted"] == 3
    assert limited["limits"] == ["vendor_budget_exhausted"]

    assert summarize_coverage(stages, 0, [])["status"] == "completed"
    assert summarize_coverage({**stages, "fetch": {"fetched": 0}}, 0, [])["status"] == "nothing_fetched"
    assert summarize_coverage({}, 0, [])["status"] == "unknown"
