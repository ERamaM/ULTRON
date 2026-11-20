"""`discover ats` stops calling a vendor host that keeps answering 429/403 (scenario S5 balanced, session 2)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from ultron import http_factory, profiles, runs, seeds
from ultron.stages.discover import discover_ats

FIX = Path(__file__).resolve().parents[1] / "fixtures"
LEVER_BOARD = (FIX / "ats" / "lever_board.json").read_bytes()


@pytest.fixture
def pack_dir(tmp_path, monkeypatch):
    path = tmp_path / "packs"
    path.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: path)
    profiles.install_example()
    return path


def test_rate_limited_vendor_host_is_blocked_for_the_rest_of_the_stage(pack_dir):
    workable = [f"acme{i}" for i in range(8)]
    rows = [{"company": b.title(), "ats": "workable", "board_identifier": b, "priority": 10 + i,
             "sectors": ["technology"]} for i, b in enumerate(workable)]
    rows.append({"company": "Lever Co", "ats": "lever", "board_identifier": "leverco", "priority": 90,
                 "sectors": ["technology"]})
    (pack_dir / "t.yaml").write_text(yaml.safe_dump({"pack": {"id": "t", "usage": "active"}, "seeds": rows}),
                                     encoding="utf-8")
    hits: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        hits[request.url.host] = hits.get(request.url.host, 0) + 1
        if request.url.host == "apply.workable.com":
            return httpx.Response(429)
        if request.url.host == "api.lever.co":
            return httpx.Response(200, content=LEVER_BOARD)
        return httpx.Response(404)

    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    try:
        run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced", {"max_seeds_per_run": 20})
        discover_ats(run_id)
    finally:
        http_factory.reset_test_overrides()
    errors = [json.loads(line) for line in
              (runs.run_dir(run_id) / "seed_errors.jsonl").read_text(encoding="utf-8").splitlines()]
    by_seed = {e["seed_id"]: e for e in errors}
    # Once the host is blocked, later boards on it are skipped without a request, and other hosts still run.
    assert hits["apply.workable.com"] <= 3
    assert hits["api.lever.co"] >= 1
    blocked = [s for s, e in by_seed.items() if e.get("error") == "host_blocked"]
    assert len(blocked) >= 5 and all(by_seed[s].get("skipped") for s in blocked)
