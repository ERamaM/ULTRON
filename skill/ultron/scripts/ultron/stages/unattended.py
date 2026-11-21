"""Unattended mode: one deterministic pass with no web search and no agent evaluations."""

from __future__ import annotations

from ultron import seeds
from ultron.seeds import rounds as seed_rounds
from ultron.stages.discover import discover_ats
from ultron.stages.feeds import PROPOSALS_FILE, discover_feeds
from ultron.stages.fetch import fetch
from ultron.stages.filter import filter_run
from ultron.stages.finalize import finalize
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, VERIFY_VENDOR_SHARE, client_for_run
from ultron.storage import runs


def verify_feed_companies(run_id: str) -> dict:
    """Verify company names found by feeds (up to ``new_companies_per_run``) and store the boards found."""
    run = runs.load_run(run_id)
    settings = run["settings"]
    rows = runs.read_jsonl(runs.run_dir(run_id) / PROPOSALS_FILE)[: settings["new_companies_per_run"]]
    if not rows:
        return {"tried": 0, "verified": 0}
    # Like ``seeds verify``: probing tries many slugs on few ATS hosts, so only the global budget bounds it.
    client = client_for_run(
        run, max_requests_per_host=settings["max_pages"], block_threshold=HOST_BLOCK_THRESHOLD,
        vendor_share=VERIFY_VENDOR_SHARE,
    )
    keys = ("company", "domain", "ats", "board", "careers_url", "source_url")
    verified = []
    unconfirmed_seeds = []
    tried = 0
    page_hops = seeds.PageHops()
    for row in rows:
        tried += 1
        result = seeds.verify_proposal(
            seeds.Proposal(**{k: str(row.get(k) or "").strip() for k in keys}),
            client=client, timeout=settings["ats_api_timeout"], page_hops=page_hops,
        )
        if result.status == "verified" and result.seed:
            verified.append(seeds.as_feed_company(result.seed))
        elif (unconfirmed := seeds.stored_seed(result)) is not None:
            unconfirmed_seeds.append(unconfirmed)  # tagged guessed board, low priority (seed_yield decides)
            if result.reason == "budget_exhausted":
                break
        elif result.reason == "budget_exhausted":
            break
    added = 0
    if verified or unconfirmed_seeds:
        try:
            added = seeds.add_seeds([*verified, *unconfirmed_seeds], profile_slug=run["profile_slug"])
        except seeds.SeedFileError:
            added = 0
    return {"tried": tried, "verified": len(verified), "unconfirmed": len(unconfirmed_seeds), "seeds_added": added}


def run_unattended(profile_slug: str, preset: str, overrides: dict | None = None) -> dict:
    """new -> discover_feeds -> discover_ats -> fetch -> filter -> finalize; ambiguous postings stay ``unsure``."""
    run_id = runs.new_run(profile_slug, preset, overrides)
    runs.mark_stage(run_id, "new")
    stages = {"discover_feeds": discover_feeds(run_id)}
    stages["feed_companies"] = verify_feed_companies(run_id)
    # Unattended runs cannot generate seeds: report the gap instead of running thin silently.
    stages["seed_coverage"] = seed_rounds.coverage(runs.load_run(run_id))
    stages["discover"] = discover_ats(run_id)
    stages["fetch"] = fetch(run_id)
    stages["filter"] = filter_run(run_id)
    result = finalize(run_id)
    return {"run_id": run_id, "stages": stages, **result}
