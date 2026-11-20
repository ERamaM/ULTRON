"""Feed links to a whole job board or a company page are routed to seeds, not fetched as postings."""

from __future__ import annotations

import httpx
import pytest

from ultron import http_factory, profiles, runs, seeds
from ultron.stages import feeds as feeds_stage


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://jobs.eu.lever.co/examplecorp", "board_root"),
        ("https://jobs.lever.co/examplecorp/", "board_root"),
        ("https://boards.greenhouse.io/examplecorp", "board_root"),
        ("https://jobs.ashbyhq.com/examplecorp", "board_root"),
        ("https://apply.workable.com/examplecorp/", "board_root"),
        ("https://jobs.workable.com/company/AbC123xyz/jobs-at-examplecorp", "board_root"),
        ("https://examplecorp.teamtailor.com/", "board_root"),
        ("https://examplecorp.bamboohr.com/careers", "board_root"),
        # A tenant-subdomain slug can be a posting (Homerun): fetched as before.
        ("https://examplecorp.homerun.co/senior-data-engineer", "unknown"),
        ("https://www.examplecorp.test", "company_page"),
        ("https://examplecorp.test/careers", "company_page"),
        ("https://examplecorp.test/company/careers/", "company_page"),
        ("https://examplecorp.test/en/jobs", "company_page"),
        ("https://jobs.eu.lever.co/examplecorp/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0", "posting"),
        ("https://boards.greenhouse.io/examplecorp/jobs/4012345", "posting"),
        ("https://apply.workable.com/examplecorp/j/AB12CD34EF/", "posting"),
        ("https://examplecorp.teamtailor.com/jobs/123456-data-engineer", "posting"),
        ("https://examplecorp.test/careers?gh_jid=4012345", "unknown"),  # a query is never a careers index
        # Unknown shapes are still fetched as today.
        ("https://examplecorp.test/careers/senior-data-engineer", "unknown"),
        ("https://examplecorp.wd3.myworkdayjobs.com/en-US/External", "unknown"),
    ],
)
def test_feed_link_kind(url, kind):
    assert feeds_stage.feed_link_kind(url) == kind


def test_direct_ats_url_prefers_a_posting_over_a_board_root():
    row = {
        "url": "https://www.arbeitnow.com/view/data-engineer-examplecorp-1",
        "description": (
            "See all roles at https://jobs.eu.lever.co/examplecorp and apply at "
            "https://jobs.eu.lever.co/examplecorp/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
        ),
    }
    assert feeds_stage._direct_ats_url(row) == "https://jobs.eu.lever.co/examplecorp/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
    assert feeds_stage._direct_ats_url({"description": "Board: https://jobs.eu.lever.co/examplecorp"}) == ""


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: empty)


def _row(title, company, url, description=""):
    return {"title": title, "company_name": company, "url": url, "location": "Remote, Europe",
            "remote": True, "description": description}


def test_board_roots_and_company_pages_become_seeds_and_hints_not_candidates(monkeypatch):
    rows = [
        # An aggregator row whose only ATS link is the employer's whole board (modelled on a live feed row).
        _row("Senior Machine Learning Engineer", "Examplecorp Group", "https://www.arbeitnow.com/view/ml-1",
             "More roles: https://jobs.eu.lever.co/examplecorpgroup"),
        # A company row pointing at a company home page and a careers index.
        _row("Senior Machine Learning Engineer", "Sampleworks", "https://www.sampleworks.test"),
        _row("Senior Machine Learning Engineer", "Demoware", "https://demoware.test/careers"),
        # A real posting is still a candidate.
        _row("Senior Machine Learning Engineer", "Postingco",
             "https://jobs.lever.co/postingco/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"),
    ]
    source = {"id": "synthetic", "parser": "synthetic", "enabled": True}
    monkeypatch.setattr(feeds_stage, "load_sources", lambda: [source])
    monkeypatch.setitem(feeds_stage.COLLECTORS, "synthetic", lambda client, src, timeout: (rows, []))
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda request: pytest.fail(f"unexpected request {request.url}")),
        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None, min_interval_per_host=0.0,
    )
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")

    summary = feeds_stage.discover_feeds(run_id)

    candidates = runs.read_jsonl(runs.run_dir(run_id) / "candidates.jsonl")
    assert [c["source_url"] for c in candidates] == [
        "https://jobs.lever.co/postingco/0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
    ]
    assert summary["feed_links_not_postings"] == 2  # home page + careers index; the board root row is an aggregator row
    inventory = {s.seed_id for s in seeds.load_inventory(profiles.EXAMPLE_SLUG)}
    assert "lever:examplecorpgroup" in inventory
    hints = {p["company"] for p in runs.read_jsonl(runs.run_dir(run_id) / feeds_stage.PROPOSALS_FILE)}
    assert {"Sampleworks", "Demoware"} <= hints
