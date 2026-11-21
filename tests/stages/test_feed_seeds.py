"""Feed-derived seeds and company proposals: relevance gating and frequency ranking."""

from __future__ import annotations

import httpx
import pytest

from ultron import seeds
from ultron.profiles import store as profiles
from ultron.stages import feeds as feeds_stage
from ultron.stages import http_factory
from ultron.stages.unattended import verify_feed_companies
from ultron.storage import runs


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)


def _job(i: int, title: str, company: str, url: str) -> dict:
    return {"id": i, "title": title, "company_name": company, "url": url, "candidate_required_location": "Remote"}


def _run_with(monkeypatch, sources: list[dict], responses: dict[str, object]) -> str:
    monkeypatch.setattr(feeds_stage, "load_sources", lambda: sources)

    def handler(request: httpx.Request) -> httpx.Response:
        body = responses.get(request.url.host)
        if body is None:
            return httpx.Response(404)
        return httpx.Response(200, json=body) if isinstance(body, dict) else httpx.Response(200, text=body)

    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    return runs.new_run(profiles.EXAMPLE_SLUG, "balanced")


REMOTIVE = [{"id": "remotive", "parser": "remotive", "url": "https://remotive.com/api/remote-jobs", "enabled": True}]
ML = "Senior Machine Learning Engineer"


def test_posting_feed_boards_need_a_relevant_row_to_become_seeds(monkeypatch):
    payload = {"jobs": [
        _job(1, ML, "Goodco", "https://boards.greenhouse.io/goodco/jobs/1"),
        _job(2, "Sales Director", "Salesco", "https://boards.greenhouse.io/salesco/jobs/2"),
        _job(3, "Account Executive", "Salesco", "https://boards.greenhouse.io/salesco/jobs/3"),
    ]}
    run_id = _run_with(monkeypatch, REMOTIVE, {"remotive.com": payload})
    out = feeds_stage.discover_feeds(run_id)
    boards = {s.board_identifier for s in seeds.load_inventory(profiles.EXAMPLE_SLUG)}
    assert boards == {"goodco"} and out["seeds_added"] == 1
    assert out["feed_boards_irrelevant"] == 1


def test_feed_company_proposals_are_ranked_by_frequency_before_the_cut(monkeypatch):
    rows = [_job(1, ML, "Rareco", "https://rareco.example/jobs/1")]
    rows += [_job(10 + i, ML, "Busyco", f"https://busyco.example/jobs/{i}") for i in range(3)]
    rows += [_job(20 + i, ML, "Midco", f"https://midco.example/jobs/{i}") for i in range(2)]
    rows += [_job(30, "Sales Director", "Busyco", "https://busyco.example/jobs/sales")]  # not relevant: not counted
    run_id = _run_with(monkeypatch, REMOTIVE, {"remotive.com": {"jobs": rows}})
    feeds_stage.discover_feeds(run_id)
    props = runs.read_jsonl(runs.run_dir(run_id) / seeds.FEED_PROPOSALS_FILE)
    assert [p["company"] for p in props] == ["Busyco", "Midco", "Rareco"]


def test_a_later_row_fills_the_company_domain(monkeypatch):
    rows = [_job(1, ML, "Zentro Labs", "https://github.com/zentro/jobs"),
            _job(2, ML, "Zentro Labs", "https://careers.zentrolabs.io/roles/2")]
    run_id = _run_with(monkeypatch, REMOTIVE, {"remotive.com": {"jobs": rows}})
    feeds_stage.discover_feeds(run_id)
    (prop,) = runs.read_jsonl(runs.run_dir(run_id) / seeds.FEED_PROPOSALS_FILE)
    assert (prop["domain"], prop["source_url"]) == ("zentrolabs.io", "https://github.com/zentro/jobs")


def test_company_list_boards_are_still_saved(monkeypatch):
    sources = [{"id": "remote_es_remotes", "parser": "remote_es_remotes", "enabled": True, "company_list": True,
                "url": "https://raw.githubusercontent.com/remote-es/remotes/master/README.md"}]
    readme = "* Listco (Madrid) [Open positions](https://boards.greenhouse.io/listco)\n"
    run_id = _run_with(monkeypatch, sources, {"raw.githubusercontent.com": readme})
    assert feeds_stage.discover_feeds(run_id)["seeds_added"] == 1
    assert {s.board_identifier for s in seeds.load_inventory(profiles.EXAMPLE_SLUG)} == {"listco"}


def test_unattended_stores_unconfirmed_guessed_boards_tagged(monkeypatch):
    gh = {"jobs": [{"id": 1, "title": ML, "absolute_url": "https://boards.greenhouse.io/novaco/jobs/1",
                    "location": {"name": "Remote"}}]}
    run_id = _run_with(monkeypatch, [], {"boards-api.greenhouse.io": gh})
    runs.write_jsonl(runs.run_dir(run_id) / seeds.FEED_PROPOSALS_FILE, [{"company": "Novaco"}])
    out = verify_feed_companies(run_id)
    assert (out["verified"], out["unconfirmed"], out["seeds_added"]) == (0, 1, 1)
    (stored,) = [s for s in seeds.load_inventory(profiles.EXAMPLE_SLUG) if profiles.EXAMPLE_SLUG in s.profiles]
    assert (stored.board_identifier, stored.tags, stored.priority) == (
        "novaco", ["ownership:unknown"], seeds.UNCONFIRMED_SEED_PRIORITY)
