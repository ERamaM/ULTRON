from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ultron import http_factory, profiles, runs, seeds
from ultron.cli import main
from ultron.core.models import AtsBoardSeed

FIX = Path(__file__).resolve().parent / "fixtures"
PAGE = (FIX / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: empty)


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
    rows = json.loads(Path(out["outputs"]["results_json"]).read_text(encoding="utf-8"))["rows"]
    assert any(r["evaluation_verdict"] == "keep" for r in rows)
    assert out["agent_evaluated"] == 0 and out["heuristic_fallback"] >= 1
    assert runs.load_run(out["run_id"])["stages_done"][-1] == "finalize"
    assert len([line for line in md.read_text(encoding="utf-8").splitlines() if line.startswith("|")]) >= 3


def test_run_new_still_works(capsys):
    profiles.install_example()
    assert main(["run", "new", "--profile", profiles.EXAMPLE_SLUG]) == 0
    assert "run_id" in json.loads(capsys.readouterr().out)
