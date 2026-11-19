from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ultron import http_factory, profiles, runs, seeds
from ultron.cli import main
from ultron.core.models import AtsBoardSeed
from ultron.stages import feeds as feeds_stage
from ultron.stages.discover import discover_ats
from ultron.stages.unattended import run_unattended, verify_feed_companies

FIX = Path(__file__).resolve().parents[1] / "fixtures"
FEEDS = FIX / "feeds"


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: empty)


def _read(name: str) -> bytes:
    return (FEEDS / name).read_bytes()


def make_handler(*, overrides: dict | None = None):
    overrides = overrides or {}

    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host in overrides:
            return overrides[host](request)
        if "greenhouse" in host and path.startswith("/v1/boards/acme"):
            return httpx.Response(200, content=(FIX / "ats" / "greenhouse_board.json").read_bytes())
        table = {
            "www.arbeitnow.com": "arbeitnow.json",
            "himalayas.app": "himalayas.json",
            "remotive.com": "remotive.json",
            "remoteok.com": "remoteok.json",
            "www.themuse.com": "themuse.json",
            "www.workingnomads.com": "workingnomads.json",
            "remotifyeurope.com": "remotifyeurope.html",
        }
        if host in table:
            return httpx.Response(200, content=_read(table[host]))
        if host == "weworkremotely.com":
            return httpx.Response(200, content=_read("weworkremotely.rss" if path.endswith(".rss") else "wwr_top.html"))
        if host == "jobicy.com":
            return httpx.Response(200, content=_read("jobicy_api.json" if path.startswith("/api") else "atom.xml"))
        if host == "raw.githubusercontent.com":
            return httpx.Response(200, content=_read("remote_es.md" if "remote-es" in path else "github_awesome.md"))
        if host == "hn.algolia.com":
            return httpx.Response(200, content=_read("hn_items.json" if "/items/" in path else "hn_search.json"))
        return httpx.Response(404)

    return handler


def _install(handler):
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )


def _new_run(handler, preset="balanced"):
    _install(handler)
    return runs.new_run(profiles.EXAMPLE_SLUG, preset)


def test_discover_feeds_cli_writes_candidates_seeds_and_hints(capsys):
    run_id = _new_run(make_handler(overrides={"www.themuse.com": lambda r: pytest.fail("The Muse is disabled")}))
    assert main(["discover", "feeds", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sources_failed"] == 0 and out["sources_ok"] == 13
    assert out["candidates"] >= 1 and out["seeds_added"] >= 1 and out["company_hints"] >= 1
    d = runs.run_dir(run_id)
    rows = runs.read_jsonl(d / "candidates.jsonl")
    assert rows and all(r["source_type"].startswith("feed:") for r in rows)
    # Rows whose only URL is the aggregator's own page give no candidate (MARK III harvest semantics).
    assert not any(feeds_stage.is_aggregator_host(r["domain"]) for r in rows)
    assert not any("Sales Director" in r["ats_title"] for r in rows)  # prefilter drops irrelevant titles
    props = runs.read_jsonl(d / "seed_proposals_from_feeds.jsonl")
    assert {"Nimbus", "Widgets Inc"} <= {p["company"] for p in props}
    assert "discover_feeds" in runs.load_run(run_id)["stages_done"]


def test_feed_failure_isolated(capsys):
    run_id = _new_run(make_handler(overrides={"remotive.com": lambda r: httpx.Response(500)}))
    assert main(["discover", "feeds", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["sources_failed"] == 1 and out["sources_ok"] == 12
    d = runs.run_dir(run_id)
    rows = runs.read_jsonl(d / "candidates.jsonl")
    assert rows and not any(r["source_type"] == "feed:remotive" for r in rows)
    assert any(r["source_type"] == "feed:arbeitnow" for r in rows)
    errors = runs.read_jsonl(d / "feed_errors.jsonl")
    assert errors and all(e["source"] == "remotive" for e in errors)


def test_feed_garbage_payload_is_a_failure_not_a_crash():
    run_id = _new_run(make_handler(overrides={"remoteok.com": lambda r: httpx.Response(200, text="<html>nope</html>")}))
    assert feeds_stage.discover_feeds(run_id)["sources_failed"] == 1


def test_feed_ats_url_becomes_seed():
    run_id = _new_run(make_handler())
    assert feeds_stage.discover_feeds(run_id)["seeds_added"] >= 1
    gh = [s for s in seeds.load_inventory(profiles.EXAMPLE_SLUG) if s.seed_id == "greenhouse:acme"]
    assert len(gh) == 1 and gh[0].source_type == "feed_discovered"
    assert profiles.EXAMPLE_SLUG in gh[0].profiles
    assert feeds_stage.discover_feeds(run_id)["seeds_added"] == 0  # known on the second pass


def test_rerun_is_idempotent_and_dedups():
    run_id = _new_run(make_handler())
    first = feeds_stage.discover_feeds(run_id)
    n = len(runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl"))
    second = feeds_stage.discover_feeds(run_id)
    assert first["candidates"] == second["candidates"] == n


@pytest.mark.parametrize("feeds_first", [True, False])
def test_feeds_and_ats_do_not_erase_each_other(feeds_first):
    run_id = _new_run(make_handler())
    seeds.add_seeds(
        [AtsBoardSeed(seed_id="greenhouse:acme", company="Acme", ats="greenhouse", board_identifier="acme",
                      priority=1, profiles=[profiles.EXAMPLE_SLUG])],
        profile_slug=profiles.EXAMPLE_SLUG,
    )
    order = (feeds_stage.discover_feeds, discover_ats) if feeds_first else (discover_ats, feeds_stage.discover_feeds)
    for stage in order:
        stage(run_id)
    rows = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    kinds = {r["source_type"] for r in rows}
    assert "ats_api" in kinds and any(k.startswith("feed:") for k in kinds)
    urls = [r["normalized_url"] for r in rows]
    assert len(urls) == len(set(urls))


def test_fetch_accepts_discover_feeds_alone():
    run_id = _new_run(make_handler())
    with pytest.raises(runs.StageOrderError):
        runs.require_stage(run_id, "fetch")
    feeds_stage.discover_feeds(run_id)
    runs.require_stage(run_id, "fetch")


def test_global_budget_stops_and_skips_rest():
    run_id = _new_run(make_handler(), preset="fast")
    run = runs.load_run(run_id)
    run["settings"]["max_pages"] = 2
    run["settings"]["max_seeds_per_run"] = 0
    runs._write_run(run_id, run)
    out = feeds_stage.discover_feeds(run_id)
    assert out["sources_skipped"] >= 1 and out["requests"] <= 2
    errors = runs.read_jsonl(runs.run_dir(run_id) / "feed_errors.jsonl")
    assert any(e.get("skipped") for e in errors)


def test_host_budget_skips_only_that_source():
    run_id = _new_run(make_handler())
    run = runs.load_run(run_id)
    run["settings"]["max_requests_per_host"] = 1
    runs._write_run(run_id, run)
    out = feeds_stage.discover_feeds(run_id)
    # weworkremotely.com and raw.githubusercontent.com each serve two sources: the second one is skipped
    assert out["sources_skipped"] >= 1 and out["sources_ok"] >= 8


def test_unattended_runs_feeds_first():
    _install(make_handler())
    result = run_unattended(profiles.EXAMPLE_SLUG, "balanced")
    done = runs.load_run(result["run_id"])["stages_done"]
    assert done == ["new", "discover_feeds", "discover_ats", "fetch", "filter", "finalize"]
    assert result["stages"]["discover_feeds"]["sources_ok"] == 13
    assert result["stages"]["feed_companies"]["tried"] >= 1


def test_unattended_verifies_feed_proposals_up_to_limit():
    def greenhouse(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/boards/nimbus"):
            return httpx.Response(200, content=(FIX / "ats" / "greenhouse_board.json").read_bytes())
        return httpx.Response(404)

    run_id = _new_run(make_handler(overrides={"boards-api.greenhouse.io": greenhouse}), preset="fast")
    feeds_stage.discover_feeds(run_id)
    out = verify_feed_companies(run_id)
    assert out["tried"] <= runs.load_run(run_id)["settings"]["new_companies_per_run"]
    assert out["verified"] >= 1
    assert any(s.seed_id == "greenhouse:nimbus" for s in seeds.load_inventory(profiles.EXAMPLE_SLUG))


@pytest.mark.parametrize(
    ("company", "url", "expected_domain"),
    [
        ("Acme", "https://jobs.acme.io/x", "acme.io"),
        ("Acme", "https://jobs.acme.com/roles/1", "acme.com"),
        ("Foo Corp", "https://careers.foo.io/openings", "foo.io"),
        ("Acme", "https://www.apply.boards.team.work.job.acme.co.uk/apply", "acme.co.uk"),
        ("Foo Corp", "https://github.com/acme/jobs", ""),
        ("Foo Corp", "https://gitlab.com/acme/careers", ""),
        ("Foo Corp", "https://www.linkedin.com/jobs/view/123", ""),
        ("Foo Corp", "https://news.ycombinator.com/item?id=123", ""),
        ("Foo Corp", "https://www.notion.so/acme-jobs", ""),
        ("Foo Corp", "https://acme.notion.site/careers", ""),
        ("Foo Corp", "https://medium.com/@acme/hiring", ""),
        ("Foo Corp", "https://twitter.com/acme", ""),
        ("Foo Corp", "https://x.com/acme", ""),
        ("Foo Corp", "https://facebook.com/acme", ""),
        ("Foo Corp", "https://google.com/about/careers", ""),
        ("Foo Corp", "https://docs.google.com/forms/d/1", ""),
        ("Foo Corp", "https://angel.co/company/acme", ""),
        ("Foo Corp", "https://wellfound.com/company/acme", ""),
        ("Foo Corp", "https://lever.co/customers", ""),
        ("Foo Corp", "https://greenhouse.io/blog", ""),
        ("Foo Corp", "https://apply.workable.com/", ""),
        ("Foo Corp", "https://www.arbeitnow.com/jobs/1", ""),
        ("Foo Corp", "https://bit.ly/acme-jobs", ""),
        ("Foo Corp", "https://jobs.com/acme", ""),
        ("Foo Corp", "https://careers.co.uk/acme", ""),
    ],
)
def test_feed_proposal_domain_filters_generic_and_third_party_hosts(monkeypatch, company, url, expected_domain):
    monkeypatch.setattr(
        feeds_stage,
        "load_sources",
        lambda: [{"id": "custom", "parser": "remotive", "url": "https://remotive.com/api/remote-jobs", "enabled": True}],
    )
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Senior Machine Learning Engineer",
                "company_name": company,
                "url": url,
                "candidate_required_location": "Remote",
            }
        ]
    }
    run_id = _new_run(make_handler(overrides={"remotive.com": lambda r: httpx.Response(200, json=payload)}))
    feeds_stage.discover_feeds(run_id)
    props = runs.read_jsonl(runs.run_dir(run_id) / feeds_stage.PROPOSALS_FILE)
    assert len(props) == 1
    assert props[0]["company"] == company
    assert props[0]["domain"] == expected_domain
    assert props[0]["source_url"] == url
    if expected_domain:
        assert expected_domain.split(".")[0] in seeds.slug_candidates(props[0]["company"], props[0]["domain"])
    else:
        assert seeds.slug_candidates(props[0]["company"], props[0]["domain"]) == seeds.slug_candidates(company, "")


@pytest.mark.parametrize(
    ("company", "url", "expected_domain"),
    [
        ("Foo", "https://airtable.com/shrXYZ", ""),
        ("Foo", "https://forms.gle/abc", ""),
        ("Foo", "https://calendly.com/foo", ""),
        ("Foo", "https://hiring.acme.com/x", ""),
        ("Foo", "https://www.workatastartup.com/companies/foo", ""),
        ("Foo", "https://www.youtube.com/watch?v=1", ""),
        ("Foo", "https://www.indeed.com/cmp/Foo", ""),
        ("Foo", "https://foo.airtable.com/shrXYZ", ""),
        ("X", "https://careers.x.io/openings", ""),
        ("Acme", "https://jobs.acme.io/x", "acme.io"),
        ("Acme Labs Inc", "https://www.acmelabs.com", "acmelabs.com"),
        ("Stripe", "https://stripe.com/jobs", "stripe.com"),
        ("Box", "https://www.dropbox.com/jobs", ""),
        ("Meta", "https://www.metabase.com/jobs", ""),
        ("Arc", "https://www.search.com/jobs", ""),
        ("Ada", "https://www.canada.ca/jobs", ""),
        ("Hugging Face", "https://huggingface.co/jobs", "huggingface.co"),
        ("Monzo Bank", "https://monzo.com/careers", "monzo.com"),
        ("N26", "https://n26.com/careers", "n26.com"),
        ("Mistral AI", "https://mistral.ai/careers", "mistral.ai"),
        ("Deutsche Telekom", "https://www.telekom.de/jobs", "telekom.de"),
        ("Google DeepMind", "https://deepmind.google/careers", "deepmind.google"),
        ("Revolut Ltd", "https://www.revolut.com/careers", "revolut.com"),
        ("Zalando SE", "https://jobs.zalando.com/x", "zalando.com"),
        ("Société Générale", "https://careers.societegenerale.com/x", "societegenerale.com"),
        ("Spotify", "https://www.lifeatspotify.com/jobs", "lifeatspotify.com"),
        ("Glovo", "https://www.glovoapp.com/jobs", "glovoapp.com"),
        ("Acme", "https://www.acme.co.uk/jobs", "acme.co.uk"),
    ],
)
def test_feed_proposal_domain_requires_company_match(company, url, expected_domain):
    assert feeds_stage._company_domain_from_url(url, company) == expected_domain


def test_unattended_does_not_probe_third_party_or_generic_host_slugs(monkeypatch):
    monkeypatch.setattr(
        feeds_stage,
        "load_sources",
        lambda: [{"id": "custom", "parser": "remotive", "url": "https://remotive.com/api/remote-jobs", "enabled": True}],
    )
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Senior Machine Learning Engineer",
                "company_name": "Foo",
                "url": "https://github.com/foo/jobs",
                "candidate_required_location": "Remote",
            },
            {
                "id": 2,
                "title": "Senior Machine Learning Engineer",
                "company_name": "Baz",
                "url": "https://airtable.com/shrXYZ",
                "candidate_required_location": "Remote",
            },
            {
                "id": 3,
                "title": "Senior Machine Learning Engineer",
                "company_name": "Qux",
                "url": "https://hiring.acme.com/x",
                "candidate_required_location": "Remote",
            },
            {
                "id": 4,
                "title": "Senior Machine Learning Engineer",
                "company_name": "Bar Corp",
                "url": "https://jobs.barcorp.io/x",
                "candidate_required_location": "Remote",
            },
        ]
    }
    probed_boards: list[str] = []

    def greenhouse(request: httpx.Request) -> httpx.Response:
        slug = request.url.path.split("/")[3]
        probed_boards.append(slug)
        if slug in {"github", "jobs", "airtable", "hiring", "acme", "barcorp"}:
            return httpx.Response(200, content=(FIX / "ats" / "greenhouse_board.json").read_bytes())
        return httpx.Response(404)

    run_id = _new_run(
        make_handler(
            overrides={
                "remotive.com": lambda r: httpx.Response(200, json=payload),
                "boards-api.greenhouse.io": greenhouse,
            }
        ),
        preset="balanced",
    )
    feeds_stage.discover_feeds(run_id)
    verify_feed_companies(run_id)
    inv = seeds.load_inventory(profiles.EXAMPLE_SLUG)
    assert not ({"github", "jobs", "airtable", "hiring", "acme"} & set(probed_boards))
    assert not any(s.board_identifier in {"github", "jobs", "airtable", "hiring", "acme"} for s in inv)
    bar_seed = next(s for s in inv if s.board_identifier == "barcorp")
    assert bar_seed.company == "Bar Corp"
    assert bar_seed.source_url == "https://jobs.barcorp.io/x"


def test_discover_feeds_skips_clearly_ineligible_and_ranks_preferred_region():
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Senior Backend Engineer",
                "company_name": "HybridCo",
                "url": "https://boards.greenhouse.io/hybridco/jobs/1",
                "candidate_required_location": "Hybrid - Austin, TX",
            },
            {
                "id": 2,
                "title": "Senior Backend Engineer",
                "company_name": "ExcludedCo",
                "url": "https://boards.greenhouse.io/excludedco/jobs/2",
                "candidate_required_location": "Remote - US only",
            },
            {
                "id": 3,
                "title": "Senior Backend Engineer",
                "company_name": "GlobalCo",
                "url": "https://boards.greenhouse.io/globalco/jobs/3",
                "candidate_required_location": "Remote - Worldwide",
            },
            {
                "id": 4,
                "title": "Senior Backend Engineer",
                "company_name": "LatamCo",
                "url": "https://boards.greenhouse.io/latamco/jobs/4",
                "candidate_required_location": "Remote - Brazil",
            },
        ]
    }
    import yaml

    latam = yaml.safe_load((FIX / "profiles" / "backend-latam.yaml").read_text(encoding="utf-8"))
    latam["criteria"]["location_scope"]["excluded_terms"] = ["us only"]
    slug = latam["slug"]
    _install(make_handler(overrides={"remotive.com": lambda r: httpx.Response(200, json=payload)}))
    (profiles._user_dir() / f"{slug}.yaml").parent.mkdir(parents=True, exist_ok=True)
    (profiles._user_dir() / f"{slug}.yaml").write_text(yaml.safe_dump(latam), encoding="utf-8")
    run_id = runs.new_run(slug, "balanced")
    feeds_stage.discover_feeds(run_id)
    rows = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    remotive_urls = [r["source_url"] for r in rows if r["source_type"] == "feed:remotive"]
    assert "https://boards.greenhouse.io/hybridco/jobs/1" not in remotive_urls
    assert "https://boards.greenhouse.io/excludedco/jobs/2" not in remotive_urls
    assert remotive_urls == [
        "https://boards.greenhouse.io/latamco/jobs/4",
        "https://boards.greenhouse.io/globalco/jobs/3",
    ]
