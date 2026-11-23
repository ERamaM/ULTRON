"""Unattended mode: one deterministic pass with no web search and no agent evaluations."""

from __future__ import annotations

from ultron import seeds
from ultron.seeds import rounds as seed_rounds
from ultron.stages.discover import SEED_ATTEMPTS_FILE, discover_ats
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


def summarize_coverage(stages: dict, roles: int, board_observations: list[dict]) -> dict:
    """Explain shortlist size using counters recorded by the unattended stages."""
    discover = stages.get("discover", {})
    feed_results = stages.get("discover_feeds", {})
    feeds = {status: feed_results.get(f"sources_{status}") for status in ("ok", "failed", "skipped")}
    fetch_summary = stages.get("fetch", {})
    filter_summary = stages.get("filter", {})
    boards = {status: discover.get(f"seeds_{status}") for status in ("completed", "partial", "failed", "skipped")}
    attempted = (boards["completed"], boards["partial"], boards["failed"])
    boards["attempted"] = sum(attempted) if None not in attempted else None
    fetched = fetch_summary.get("fetched")
    pending = filter_summary.get("ambiguous")
    limits = {
        row["reason"] for row in board_observations
        if row.get("reason", "").endswith(("_budget", "_budget_exhausted", "page_limit"))
    }
    if fetch_summary.get("capped"):
        limits.add("max_pages_limit")
    if stages.get("feed_companies", {}).get("deferred"):
        limits.add("feed_verification_budget")
    if fetch_summary.get("stopped"):
        limits.add(fetch_summary["stopped"])
    if None in (*boards.values(), *feeds.values(), fetched, pending):
        status = "unknown"
    elif (any(boards[key] for key in ("partial", "failed", "skipped")) or limits
          or feeds["failed"] or feeds["skipped"] or fetch_summary.get("failed")):
        status = "limited"
    elif not fetched and (discover.get("candidates") or not boards["completed"]):
        status = "nothing_fetched"
    else:
        status = "completed"
    return {
        "status": status, "roles": roles, "boards": boards, "postings_fetched": fetched,
        "pending_ambiguous": pending, "feeds": feeds, "limits": sorted(limits),
        "fitting_board_deficit": stages.get("seed_coverage", {}).get("seed_deficit"),
    }


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
    observations = runs.read_jsonl(runs.run_dir(run_id) / SEED_ATTEMPTS_FILE)
    coverage = summarize_coverage(stages, result["roles"], observations)
    runs.record_coverage(run_id, coverage)
    return {"run_id": run_id, "stages": stages, "coverage": coverage, **result}
