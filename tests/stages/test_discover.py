from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ultron import http_factory, profiles, runs, seeds
from ultron.cli import main
from ultron.core.models import AtsBoardSeed

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "ats"


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: empty)


def _seed(ats, board, priority=100):
    return AtsBoardSeed(seed_id=f"{ats}:{board}", company=board.title(), ats=ats, board_identifier=board,
                        priority=priority, profiles=[profiles.EXAMPLE_SLUG])


def _setup(handler, seed_list):
    profiles.install_example()
    seeds.add_seeds(seed_list, profile_slug=profiles.EXAMPLE_SLUG)
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    return runs.new_run(profiles.EXAMPLE_SLUG, "balanced")


def _handler(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if "greenhouse" in host and "/acme/" in path:
        return httpx.Response(200, content=(FIX / "greenhouse_board.json").read_bytes())
    if host == "api.lever.co" and "/acme" in path:
        return httpx.Response(200, content=(FIX / "lever_board.json").read_bytes())
    return httpx.Response(404)


def test_discover_writes_candidates_and_survives_failing_seed(capsys):
    run_id = _setup(_handler, [_seed("greenhouse", "acme"), _seed("lever", "acme"), _seed("greenhouse", "ghost")])
    assert main(["discover", "ats", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["seeds_failed"] == 1 and out["seeds_queried"] == 3 and out["candidates"] >= 2
    d = runs.run_dir(run_id)
    rows = runs.read_jsonl(d / "candidates.jsonl")
    assert {r["source_key"] for r in rows} == {"greenhouse:acme", "lever:acme"}
    assert all(r["source_type"] == "ats_api" and r["ats_title"] for r in rows)
    errors = runs.read_jsonl(d / "seed_errors.jsonl")
    assert len(errors) == 1 and errors[0]["seed_id"] == "greenhouse:ghost"
    assert "discover_ats" in runs.load_run(run_id)["stages_done"]


def test_discover_applies_prefilter():
    def handler(request):
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Office Manager", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
             "location": {"name": "Madrid"}},
            {"id": 2, "title": "Senior Machine Learning Engineer",
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "location": {"name": "Remote - Spain"}},
        ]})

    run_id = _setup(handler, [_seed("greenhouse", "acme")])
    from ultron.stages.discover import discover_ats

    discover_ats(run_id)
    titles = [r["ats_title"] for r in runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")]
    assert "Office Manager" not in titles and "Senior Machine Learning Engineer" in titles


def test_add_candidates_dedups_and_proposes_seed(tmp_path):
    from ultron.stages.candidates import add_candidates
    from ultron.stages.discover import discover_ats

    run_id = _setup(_handler, [_seed("greenhouse", "acme")])
    discover_ats(run_id)
    existing = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")[0]["source_url"]
    src = tmp_path / "hits.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in [
        {"url": existing, "title": "Dup"},
        {"url": "https://jobs.ashbyhq.com/newco/123", "title": "AI Engineer", "company": "NewCo"},
        {"url": "not a url"},
    ]), encoding="utf-8")
    res = add_candidates(run_id, src)
    assert res == {"added": 1, "duplicates": 1, "rejected": 1, "seed_proposals": 1}
    proposals = runs.read_jsonl(runs.run_dir(run_id) / "seed_proposals_from_search.jsonl")
    assert proposals[0]["careers_url"] == "https://jobs.ashbyhq.com/newco/123" and proposals[0]["ats"] == "ashby"
    rows = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    assert any(r["source_type"] == "web_search" for r in rows)


def test_discover_global_budget_stop_records_skipped():
    from ultron.stages.discover import discover_ats

    run_id = _setup(_handler, [_seed("greenhouse", "acme", 1), _seed("greenhouse", "b", 2), _seed("greenhouse", "c", 3)])
    http_factory.set_test_overrides(max_requests=1)
    res = discover_ats(run_id)
    assert res["seeds_skipped"] == 2 and res["seeds_failed"] == 0 and res["candidates"] >= 1
    errors = runs.read_jsonl(runs.run_dir(run_id) / "seed_errors.jsonl")
    assert [e["seed_id"] for e in errors] == ["greenhouse:b", "greenhouse:c"]
    assert all(e["error"] == "request_budget_exhausted" for e in errors)


def test_discover_malformed_payload_seed_is_failure():
    from ultron.stages.discover import discover_ats

    def handler(request):
        if "/bad/" in request.url.path:
            return httpx.Response(200, text="<html>not json</html>")
        return _handler(request)

    run_id = _setup(handler, [_seed("greenhouse", "bad", 1), _seed("greenhouse", "acme", 2)])
    res = discover_ats(run_id)
    assert res["seeds_failed"] == 1 and res["seeds_skipped"] == 0 and res["candidates"] >= 1


def test_discover_european_rank_bonus_only_for_european_scope():
    import yaml

    def handler(request):
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Senior Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
             "location": {"name": "Brazil"}},
            {"id": 2, "title": "Senior Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
             "location": {"name": "Europe"}},
        ]})

    from ultron.stages.discover import discover_ats

    latam = yaml.safe_load((FIX.parent / "profiles" / "backend-latam.yaml").read_text(encoding="utf-8"))
    eu = {**latam, "criteria": {**latam["criteria"], "location_scope": {"preferred": ["Remote EU"]}}}
    first_kept = {}
    for name, profile in (("latam", latam), ("eu", eu)):
        run_id = _setup(handler, [_seed("greenhouse", "acme")])
        run = runs.load_run(run_id)
        run["profile_snapshot"] = profile
        run["settings"]["ats_api_max_jobs_per_seed"] = 1
        runs._write_run(run_id, run)
        discover_ats(run_id)
        [row] = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
        first_kept[name] = row["source_url"]
    assert first_kept["latam"].endswith("/jobs/1")
    assert first_kept["eu"].endswith("/jobs/2")


def test_discover_cleans_entities_in_titles():
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"jobs": [{"id": 1, "title": "Senior Data Scientist &amp; ML &#8211; Remote", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
                          "location": {"name": "Espa&#241;a"}}]}
        return httpx.Response(200, json=body)

    run_id = _setup(handler, [_seed("greenhouse", "acme")])
    assert main(["discover", "ats", "--run", run_id]) == 0
    rows = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    assert rows and rows[0]["ats_title"] == "Senior Data Scientist & ML – Remote" and rows[0]["hit_title"] == "Senior Data Scientist & ML – Remote"
    assert rows[0]["ats_location"] == "España"


def test_add_candidates_cleans_title_and_company(tmp_path):
    from ultron.stages.candidates import add_candidates

    run_id = _setup(_handler, [])
    src = tmp_path / "hits.jsonl"
    src.write_text(json.dumps({"url": "https://jobs.ashbyhq.com/newco/9", "title": "R&amp;D Lead",
                               "company": "EspaÃ±a &amp; Co"}), encoding="utf-8")
    add_candidates(run_id, src)
    row = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")[0]
    assert row["ats_title"] == "R&D Lead" and row["ats_company"] == "España & Co"


def test_discover_verifies_and_queries_search_proposals_without_refetch(tmp_path):
    from ultron.stages.candidates import add_candidates
    from ultron.stages.discover import discover_ats

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "/newco/" in request.url.path:
            return httpx.Response(200, json={"jobs": [
                {"id": 123, "title": "Senior AI Engineer", "absolute_url": "https://boards.greenhouse.io/newco/jobs/123",
                 "location": {"name": "Remote Europe"}},
                {"id": 456, "title": "Senior ML Engineer", "absolute_url": "https://boards.greenhouse.io/newco/jobs/456",
                 "location": {"name": "Remote EU"}},
            ]})
        return _handler(request)

    run_id = _setup(handler, [])
    src = tmp_path / "hits.jsonl"
    src.write_text(json.dumps({
        "url": "https://boards.greenhouse.io/newco/jobs/123", "title": "Senior AI Engineer", "company": "NewCo",
    }), encoding="utf-8")
    assert add_candidates(run_id, src)["seed_proposals"] == 1

    res = discover_ats(run_id)
    assert res["search_proposals_verified"] == 1
    assert res["search_proposals_added"] == 1
    assert len(calls) == 1 and "/newco/" in calls[0]
    assert any(s.seed_id == "greenhouse:newco" for s in seeds.load_inventory(profiles.EXAMPLE_SLUG))
    rows = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    assert any(r["source_type"] == "ats_api" and r["source_url"] == "https://boards.greenhouse.io/newco/jobs/456" for r in rows)
    assert any(r["source_type"] == "web_search" and r["source_url"] == "https://boards.greenhouse.io/newco/jobs/123" for r in rows)

    # A second discover_ats on the same run skips re-verifying the already-saved seed.
    res2 = discover_ats(run_id)
    assert res2["search_proposals_verified"] == 0
    assert res2["search_proposals_added"] == 0


def test_discover_ignores_invalid_search_proposals(tmp_path):
    from ultron.stages.candidates import add_candidates
    from ultron.stages.discover import discover_ats

    def handler(request: httpx.Request) -> httpx.Response:
        if "/missing/" in request.url.path:
            return httpx.Response(404)
        if "/goodco/" in request.url.path:
            return httpx.Response(200, json={"jobs": [
                {"id": 1, "title": "Senior ML Engineer", "absolute_url": "https://boards.greenhouse.io/goodco/jobs/1",
                 "location": {"name": "Remote EU"}},
            ]})
        return _handler(request)

    run_id = _setup(handler, [])
    src = tmp_path / "hits.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in [
        {"url": "https://boards.greenhouse.io/missing/jobs/9", "title": "AI Engineer", "company": "MissingCo"},
        {"url": "https://boards.greenhouse.io/goodco/jobs/1", "title": "Senior ML Engineer", "company": "GoodCo"},
    ]), encoding="utf-8")
    assert add_candidates(run_id, src)["seed_proposals"] == 2

    res = discover_ats(run_id)
    assert res["search_proposals_verified"] == 1
    assert res["search_proposals_added"] == 1
    inv_ids = {s.seed_id for s in seeds.load_inventory(profiles.EXAMPLE_SLUG)}
    assert "greenhouse:goodco" in inv_ids
    assert "greenhouse:missing" not in inv_ids


def test_search_verification_continues_after_host_budget(tmp_path):
    from ultron.stages.candidates import add_candidates
    from ultron.stages.discover import discover_ats

    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        fixture = "greenhouse_board.json" if "greenhouse" in request.url.host else "lever_board.json"
        return httpx.Response(200, content=(FIX / fixture).read_bytes())

    run_id = _setup(handler, [])
    run = runs.load_run(run_id)
    run["settings"]["max_requests_per_host"] = 1
    runs._write_run(run_id, run)
    source = tmp_path / "hits.jsonl"
    runs.write_jsonl(source, [
        {"url": "https://boards.greenhouse.io/one/jobs/1"},
        {"url": "https://boards.greenhouse.io/two/jobs/2"},
        {"url": "https://jobs.lever.co/three/12345678-1234-1234-1234-123456789abc"},
    ])
    assert add_candidates(run_id, source)["seed_proposals"] == 3

    result = discover_ats(run_id)
    assert result["search_proposals_verified"] == 2
    assert hosts == ["boards-api.greenhouse.io", "api.lever.co"]


def test_add_candidates_rejects_malformed_url_without_losing_batch(tmp_path, capsys):
    run_id = _setup(_handler, [])
    source = tmp_path / "hits.jsonl"
    urls = ["https://example.org/jobs/1", "https://[invalid", "https://example.org/jobs/2"]
    runs.write_jsonl(source, [{"url": url} for url in urls])

    assert main(["candidates", "add", "--run", run_id, "--from", str(source)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["rejected"] == 1
    assert result["added"] == 2
    assert [row["source_url"] for row in runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")] == urls[::2]


def test_discover_skips_clearly_ineligible_and_ranks_eu_remote_first():
    from ultron.stages.discover import discover_ats

    def handler(request):
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Senior ML Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
             "location": {"name": "San Francisco, CA"}},
            {"id": 2, "title": "Senior ML Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
             "location": {"name": "Hybrid - Berlin"}},
            {"id": 3, "title": "Senior ML Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/3",
             "location": {"name": "United Kingdom (Remote)"}},
            {"id": 4, "title": "Senior ML Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/4",
             "location": {"name": "Remote - Europe"}},
        ]})

    run_id = _setup(handler, [_seed("greenhouse", "acme")])
    res = discover_ats(run_id)
    assert res["candidates"] == 2
    urls = [r["source_url"] for r in runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")]
    assert urls == [
        "https://boards.greenhouse.io/acme/jobs/4",
        "https://boards.greenhouse.io/acme/jobs/3",
    ]


def test_discover_puts_active_boards_before_zero_posting_boards_and_isolates_verify_cap(tmp_path):
    from ultron.stages.candidates import add_candidates
    from ultron.stages.discover import discover_ats

    def handler(request: httpx.Request) -> httpx.Response:
        if "/emptyco/" in request.url.path:
            return httpx.Response(200, json={"jobs": []})
        if "/activeco/" in request.url.path or "/searchco/" in request.url.path:
            return httpx.Response(200, content=(FIX / "greenhouse_board.json").read_bytes())
        return httpx.Response(404)

    empty = AtsBoardSeed(
        seed_id="greenhouse:emptyco", company="EmptyCo", ats="greenhouse", board_identifier="emptyco",
        priority=10, source_type="agent_generated", profiles=[profiles.EXAMPLE_SLUG],
        verification={"postings": 0},
    )
    active = AtsBoardSeed(
        seed_id="greenhouse:activeco", company="ActiveCo", ats="greenhouse", board_identifier="activeco",
        priority=100, source_type="agent_generated", profiles=[profiles.EXAMPLE_SLUG],
        verification={"postings": 5},
    )
    run_id = _setup(handler, [empty, active])
    run = runs.load_run(run_id)
    run["settings"]["max_requests_per_host"] = 1
    runs._write_run(run_id, run)

    src = tmp_path / "hits.jsonl"
    src.write_text(json.dumps({"url": "https://boards.greenhouse.io/searchco/jobs/1", "title": "Senior ML Engineer"}), encoding="utf-8")
    add_candidates(run_id, src)

    res = discover_ats(run_id)
    assert res["search_proposals_verified"] == 1
    rows = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    assert any(r["source_key"] == "greenhouse:activeco" for r in rows)
    assert not any(r["source_key"] == "greenhouse:emptyco" for r in rows)


def test_discover_skips_non_eu_country_and_us_state_suffixes_in_title():
    from ultron.stages.discover import discover_ats

    def handler(request):
        return httpx.Response(200, json={"jobs": [
            {"id": 1, "title": "Senior ML Engineer - Albuquerque, NM, USA",
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Remote"}},
            {"id": 2, "title": "Senior ML Engineer - Ahmedabad, India",
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "location": {"name": "Remote"}},
            {"id": 3, "title": "Senior ML Engineer - Aarhus, Denmark",
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/3", "location": {"name": "Remote"}},
            {"id": 4, "title": "Senior ML Engineer - USA",
             "absolute_url": "https://boards.greenhouse.io/acme/jobs/4", "location": {"name": "Remote"}},
        ]})

    run_id = _setup(handler, [_seed("greenhouse", "acme")])
    res = discover_ats(run_id)
    assert res["candidates"] == 1
    [row] = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    assert row["source_url"].endswith("/jobs/3")


def test_discover_queries_past_workday_verification_sample(tmp_path):
    from ultron.stages.candidates import add_candidates
    from ultron.stages.discover import discover_ats

    offsets = []
    board = "https://acme.wd5.myworkdayjobs.com/External"

    def handler(request: httpx.Request) -> httpx.Response:
        offset = json.loads(request.content)["offset"]
        offsets.append(offset)
        jobs = [
            {"title": "Sales Associate", "externalPath": f"/job/Sales_{i}", "locationsText": "Spain"}
            for i in range(offset, min(41, offset + 20))
        ]
        if offset == 40:
            jobs[0].update(title="Senior ML Engineer", externalPath="/job/ML-Engineer_R-41", locationsText="Remote EU")
        return httpx.Response(200, json={"total": 41 if offset == 0 else 0, "jobPostings": jobs})

    run_id = _setup(handler, [])
    src = tmp_path / "hits.jsonl"
    src.write_text(json.dumps({"url": f"{board}/job/Old-Listing_R-1", "company": "Acme"}), encoding="utf-8")
    assert add_candidates(run_id, src)["seed_proposals"] == 1

    result = discover_ats(run_id)
    assert result["search_proposals_verified"] == 1
    assert offsets == [0, 20, 0, 20, 40]
    rows = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    assert any(row["source_url"] == f"{board}/job/ML-Engineer_R-41" for row in rows)
