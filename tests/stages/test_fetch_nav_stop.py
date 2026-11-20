"""Global stop of the fetch stage when navigation (transport) failures dominate."""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest

from ultron import http_factory, profiles, runs, seeds
from ultron.core.models import CandidateUrl
from ultron.stages import fetch as fetch_stage
from ultron.stages.fetch import fetch

PAGE = (Path(__file__).resolve().parents[1] / "fixtures" / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: empty)


def _run(handler, max_pages: int | None = None) -> str:
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0, max_requests=10_000,
    )
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    if max_pages is not None:
        run = runs.load_run(run_id)
        run["settings"]["max_pages"] = max_pages
        runs._write_run(run_id, run)
    return run_id


def _ats(board: str, n: int) -> list[CandidateUrl]:
    return [
        CandidateUrl(query_id=f"greenhouse:{board}", query="", source_url=f"https://boards.greenhouse.io/{board}/jobs/{i}",
                     normalized_url=f"https://boards.greenhouse.io/{board}/jobs/{i}", domain="boards.greenhouse.io",
                     source_type="ats_api", source_key=f"greenhouse:{board}")
        for i in range(1, n + 1)
    ]


def _start(run_id: str, candidates: list[CandidateUrl]) -> Path:
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", candidates)
    runs.mark_stage(run_id, "discover_ats")
    return d


def _network_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("network unreachable", request=request)


def test_global_stop_when_navigation_failures_dominate(monkeypatch):
    monkeypatch.setattr(fetch_stage, "NAV_FAILURE_STOP_THRESHOLD", 5)
    run_id = _run(_network_down, max_pages=100)
    d = _start(run_id, [CandidateUrl(query_id=f"q{i}", query="", source_url=f"https://h{i}.example.org/job/{i}",
                                     normalized_url=f"https://h{i}.example.org/job/{i}", domain=f"h{i}.example.org")
                        for i in range(40)])
    res = fetch(run_id)
    assert res["stopped"] == "navigation_failures"
    assert res["navigation_failures"] == 5 and res["failed"] == 40
    errors = runs.read_jsonl(d / "fetch_errors.jsonl")
    assert sum(1 for e in errors if e["error"].startswith("http_error")) == 5
    assert sum(1 for e in errors if e["error"] == "skipped_navigation_failures") == 35
    # Only the attempted URLs reached the transport.
    assert res["requests"] <= 5 * 3


def test_no_global_stop_when_failures_are_a_minority(monkeypatch):
    monkeypatch.setattr(fetch_stage, "NAV_FAILURE_STOP_THRESHOLD", 3)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host.startswith("down"):
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, text=PAGE)

    run_id = _run(handler, max_pages=100)
    cands = []
    for i in range(12):  # one failure for every two successes: 33% < 50%
        host = f"down{i}.example.org" if i % 3 == 0 else f"up{i}.example.org"
        cands.append(CandidateUrl(query_id=f"q{i}", query="", source_url=f"https://{host}/job/{i}",
                                  normalized_url=f"https://{host}/job/{i}", domain=host))
    _start(run_id, cands)
    res = fetch(run_id)
    assert "stopped" not in res
    assert res["fetched"] == 8 and res["navigation_failures"] == 4


def test_http_status_errors_do_not_count_as_navigation_failures(monkeypatch):
    monkeypatch.setattr(fetch_stage, "NAV_FAILURE_STOP_THRESHOLD", 2)
    run_id = _run(lambda r: httpx.Response(404), max_pages=100)
    _start(run_id, _ats("a", 6))
    res = fetch(run_id)
    assert "stopped" not in res and res["navigation_failures"] == 0 and res["failed"] == 6


def test_dns_failures_count_as_navigation_failures(monkeypatch):
    # Without a network, name resolution usually fails before any connection is attempted.
    monkeypatch.setattr(fetch_stage, "NAV_FAILURE_STOP_THRESHOLD", 4)
    run_id = _run(lambda r: httpx.Response(200, text=PAGE), max_pages=100)

    def no_dns(host, port):
        raise socket.gaierror("name resolution failed")

    http_factory.set_test_overrides(resolver=no_dns)
    d = _start(run_id, _ats("a", 10))
    res = fetch(run_id)
    assert res["stopped"] == "navigation_failures" and res["navigation_failures"] == 4
    errors = [e["error"] for e in runs.read_jsonl(d / "fetch_errors.jsonl")]
    assert errors[:4] == ["url_rejected: dns_resolution_failed"] * 4
    assert errors[4:] == ["skipped_navigation_failures"] * 6
