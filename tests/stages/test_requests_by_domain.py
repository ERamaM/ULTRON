"""Network stage summaries report the busiest domains, so live runs can see budget concentration."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ultron import seeds
from ultron.core.models import AtsBoardSeed, CandidateUrl
from ultron.infra.net import HttpClient, top_hosts
from ultron.profiles import store as profiles
from ultron.stages import feeds as feeds_stage
from ultron.stages import http_factory
from ultron.stages.discover import discover_ats
from ultron.stages.fetch import fetch
from ultron.storage import runs

FIX = Path(__file__).resolve().parents[1] / "fixtures"
PAGE = (FIX / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)


def _install(handler):
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )


def test_top_hosts_merges_sorts_and_caps():
    assert top_hosts({"b.test": 2, "a.test": 2, "c.test": 5}, {"a.test": 1, "d.test": 1}, limit=3) == [
        {"host": "c.test", "requests": 5},
        {"host": "a.test", "requests": 3},
        {"host": "b.test", "requests": 2},
    ]
    assert top_hosts({}) == []


def test_http_client_counts_requests_per_host():
    client = HttpClient(max_requests=10, max_requests_per_host=10, min_interval_per_host=0.0,
                        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")),
                        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None)
    for url in ("https://one.example.test/a", "https://one.example.test/b", "https://two.example.test/"):
        client.get_text(url)
    assert client.requests_by_host() == {"one.example.test": 2, "two.example.test": 1}


def test_fetch_summary_reports_requests_by_domain():
    _install(lambda r: httpx.Response(200, text=PAGE) if r.url.path.endswith("/1") else httpx.Response(404))
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    url = "https://boards.greenhouse.io/examplecorp/jobs/1"
    runs.write_jsonl(runs.run_dir(run_id) / "candidates.jsonl", [CandidateUrl(
        query_id="greenhouse:examplecorp", query="", source_url=url, normalized_url=url,
        domain="boards.greenhouse.io", source_type="ats_api", source_key="greenhouse:examplecorp",
    ).model_dump(mode="json")])
    runs.mark_stage(run_id, "discover_ats")
    summary = fetch(run_id)
    assert summary["requests_by_domain"][0] == {"host": "boards.greenhouse.io", "requests": 1}
    assert sum(row["requests"] for row in summary["requests_by_domain"]) == summary["requests"]


def test_discover_ats_summary_reports_requests_by_domain():
    def handler(request):
        if "greenhouse" in request.url.host and "/examplecorp/" in request.url.path:
            return httpx.Response(200, content=(FIX / "ats" / "greenhouse_board.json").read_bytes())
        return httpx.Response(404)

    _install(handler)
    seeds.add_seeds([
        AtsBoardSeed(seed_id="greenhouse:examplecorp", company="Examplecorp", ats="greenhouse",
                     board_identifier="examplecorp", profiles=[profiles.EXAMPLE_SLUG]),
    ], profile_slug=profiles.EXAMPLE_SLUG)
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    summary = discover_ats(run_id)
    hosts = [row["host"] for row in summary["requests_by_domain"]]
    assert hosts and all(host == "greenhouse.io" or host.endswith(".greenhouse.io") for host in hosts)
    assert sum(row["requests"] for row in summary["requests_by_domain"]) == summary["requests"]


def test_discover_feeds_summary_reports_requests_by_domain(monkeypatch):
    def collector(client, source, timeout):
        for url in ("https://feed-a.example.test/1", "https://feed-a.example.test/2", "https://feed-b.example.test/"):
            client.get_text(url)
        return [], []

    monkeypatch.setattr(feeds_stage, "load_sources", lambda: [{"id": "synthetic", "parser": "synthetic"}])
    monkeypatch.setitem(feeds_stage.COLLECTORS, "synthetic", collector)
    _install(lambda r: httpx.Response(200, text="{}"))
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    summary = feeds_stage.discover_feeds(run_id)
    assert summary["requests_by_domain"] == [
        {"host": "feed-a.example.test", "requests": 2},
        {"host": "feed-b.example.test", "requests": 1},
    ]
