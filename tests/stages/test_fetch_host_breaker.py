"""A host that keeps answering 403/429 is skipped for the rest of the fetch stage."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ultron import http_factory, profiles, runs, seeds
from ultron.core.models import CandidateUrl
from ultron.stages import fetch as fetch_stage

PAGE = (Path(__file__).resolve().parents[1] / "fixtures" / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: empty)


def _cand(host: str, i: int) -> CandidateUrl:
    url = f"https://{host}/jobs/{i}"
    return CandidateUrl(query_id="web:1", query="", source_url=url, normalized_url=url, domain=host,
                        source_type="web_search", source_key=f"web:{host}")


def _run(handler, candidates):
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    runs.write_jsonl(runs.run_dir(run_id) / "candidates.jsonl", candidates)
    runs.mark_stage(run_id, "discover_ats")
    return run_id


@pytest.mark.parametrize("blocked_status", [403, 429])
def test_breaker_skips_host_after_consecutive_blocks(blocked_status):
    hits: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        hits[request.url.host] = hits.get(request.url.host, 0) + 1
        if request.url.host == "blocked.example.org":
            return httpx.Response(blocked_status)
        return httpx.Response(200, text=PAGE)

    n = fetch_stage.HOST_BLOCK_THRESHOLD
    blocked = [_cand("blocked.example.org", i) for i in range(n + 2)]
    run_id = _run(handler, [*blocked, _cand("ok.example.org", 1)])
    result = fetch_stage.fetch(run_id)
    errors = runs.read_jsonl(runs.run_dir(run_id) / "fetch_errors.jsonl")
    by_url = {e["url"]: e for e in errors}
    # HttpClient retries a 429 within one URL and every attempt counts, so a 429 host trips after one URL.
    requested = 1 if blocked_status == 429 else n
    assert [by_url[c.normalized_url]["error"] for c in blocked] == (
        ["inaccessible"] * requested + ["host_blocked"] * (len(blocked) - requested)
    )
    assert result["fetched"] == 1  # other hosts are unaffected
    assert hits["blocked.example.org"] == n  # 3 attempts on one URL (429) or 1 attempt on each of 3 URLs (403)


def test_breaker_resets_after_a_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # 403, 403, 200, 403, 403: never three in a row.
        return httpx.Response(200, text=PAGE) if request.url.path.endswith("/2") else httpx.Response(403)

    run_id = _run(handler, [_cand("flaky.example.org", i) for i in range(5)])
    fetch_stage.fetch(run_id)
    errors = runs.read_jsonl(runs.run_dir(run_id) / "fetch_errors.jsonl")
    assert all(e["error"] == "inaccessible" for e in errors) and len(errors) == 4
