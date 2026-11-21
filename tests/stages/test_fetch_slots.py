"""Fetch slot fairness, candidate-cap accounting and the global navigation-failure stop."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ultron import seeds
from ultron.core.models import CandidateUrl
from ultron.diagnostics import diagnose
from ultron.profiles import store as profiles
from ultron.stages import fetch as fetch_stage
from ultron.stages import http_factory
from ultron.stages.fetch import fetch
from ultron.storage import runs

PAGE = (Path(__file__).resolve().parents[1] / "fixtures" / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)


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


def _web(query: str, n: int, host: str) -> list[CandidateUrl]:
    return [
        CandidateUrl(query_id=f"web:{query}:{i}", query=query, source_url=f"https://{host}/job/{i}",
                     normalized_url=f"https://{host}/job/{i}", domain=host,
                     source_type="web_search", source_key="web_search")
        for i in range(1, n + 1)
    ]


def _start(run_id: str, candidates: list[CandidateUrl]) -> Path:
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", candidates)
    runs.mark_stage(run_id, "discover_ats")
    return d


def test_each_submitted_web_query_gets_its_own_round_robin_lane():
    run_id = _run(lambda r: httpx.Response(200, text=PAGE), max_pages=6)
    d = _start(run_id, [
        *_ats("a", 5), *_ats("b", 5),
        *_web("Senior ML Engineer remote", 3, "one.example.org"),
        *_web("site:jobs.lever.co ML Engineer", 3, "two.example.org"),
    ])
    assert fetch(run_id)["fetched"] == 6
    hosts = [r["candidate"]["domain"] for r in runs.read_jsonl(d / "postings.jsonl")]
    # Round 1: a, b, query 1, query 2; round 2 starts with a, b.
    assert hosts == ["boards.greenhouse.io", "boards.greenhouse.io", "one.example.org", "two.example.org",
                     "boards.greenhouse.io", "boards.greenhouse.io"]
    # The source key the rest of the pipeline relies on is unchanged.
    assert {r["candidate"]["source_key"] for r in runs.read_jsonl(d / "postings.jsonl")} >= {"web_search"}


def test_web_rows_without_a_query_share_one_lane():
    items = [(c, c.normalized_url) for c in [*_web("", 2, "one.example.org"), *_ats("a", 2)]]
    order = [c.domain for c, _ in fetch_stage._round_robin(items)]
    assert order == ["one.example.org", "boards.greenhouse.io", "one.example.org", "boards.greenhouse.io"]


def test_candidates_cut_by_max_pages_are_counted_and_written():
    run_id = _run(lambda r: httpx.Response(200, text=PAGE), max_pages=3)
    d = _start(run_id, _ats("a", 5))
    res = fetch(run_id)
    assert res["fetched"] == 3 and res["capped"] == 2
    capped = runs.read_jsonl(d / "fetch_capped.jsonl")
    assert [row["url"] for row in capped] == ["https://boards.greenhouse.io/a/jobs/4",
                                              "https://boards.greenhouse.io/a/jobs/5"]
    assert capped[0]["identity_key"] == "greenhouse:a:4" and capped[0]["source_key"] == "greenhouse:a"


def test_capped_file_is_written_even_when_nothing_is_capped():
    run_id = _run(lambda r: httpx.Response(200, text=PAGE), max_pages=10)
    d = _start(run_id, _ats("a", 2))
    assert fetch(run_id)["capped"] == 0
    assert (d / "fetch_capped.jsonl").is_file() and runs.read_jsonl(d / "fetch_capped.jsonl") == []


def test_diagnose_reports_candidate_cap_from_the_capped_file():
    run_id = _run(lambda r: httpx.Response(200, text=PAGE), max_pages=1)
    _start(run_id, _ats("a", 2))
    fetch(run_id)
    capped = diagnose.diagnose("https://boards.greenhouse.io/a/jobs/2", profile_slug=profiles.EXAMPLE_SLUG,
                               run_id=run_id, fetch=False)
    assert (capped.first_failed_stage, capped.reason_code) == ("candidate_cap", "candidate_cap")


def test_diagnose_does_not_guess_candidate_cap_when_the_capped_file_lacks_the_job():
    run_id = _run(lambda r: httpx.Response(200, text=PAGE), max_pages=5)
    d = _start(run_id, _ats("a", 1))
    fetch(run_id)
    # A candidate appended after fetch ran: neither fetched nor capped.
    runs.append_jsonl(d / "candidates.jsonl", _ats("late", 1))
    result = diagnose.diagnose("https://boards.greenhouse.io/late/jobs/1", profile_slug=profiles.EXAMPLE_SLUG,
                               run_id=run_id, fetch=False)
    assert (result.first_failed_stage, result.reason_code) == ("pending", "not_fetched")
