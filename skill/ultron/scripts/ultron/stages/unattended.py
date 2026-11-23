"""Unattended mode: one deterministic pass with no web search and no agent evaluations."""

from __future__ import annotations

from ultron import seeds
from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url
from ultron.seeds import rounds as seed_rounds
from ultron.stages.discover import discover_ats
from ultron.stages.feeds import PROPOSALS_FILE, discover_feeds
from ultron.stages.fetch import fetch
from ultron.stages.filter import filter_run
from ultron.stages.finalize import finalize
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, VERIFY_VENDOR_SHARE, client_for_run
from ultron.storage import runs


def _distinct_proposals(rows: list[dict]) -> list[seeds.Proposal]:
    keys = ("company", "domain", "ats", "board", "careers_url", "source_url")
    distinct = []
    seen = set()
    for row in rows:
        proposal = seeds.Proposal(**{key: str(row.get(key) or "").strip() for key in keys})
        vendor, locator = proposal.ats, proposal.board
        if not (vendor and locator):
            try:
                board = extract_seed_from_url(proposal.careers_url or proposal.source_url, proposal.company)
            except ValueError:
                board = None
            if board is not None:
                vendor, locator = board.ats, board.board_identifier
        if vendor and locator:
            key = ("board", *seed_identity(vendor, locator))
        elif proposal.company:
            key = ("company", " ".join(proposal.company.casefold().split()), proposal.domain.casefold())
        else:
            continue
        if key not in seen:
            seen.add(key)
            distinct.append(proposal)
    return distinct


def verify_feed_companies(run_id: str) -> dict:
    """Verify company names found by feeds (up to ``new_companies_per_run``) and store the boards found."""
    run = runs.load_run(run_id)
    settings = run["settings"]
    rows = runs.read_jsonl(runs.run_dir(run_id) / PROPOSALS_FILE)
    distinct = _distinct_proposals(rows)
    proposals = distinct[: settings["new_companies_per_run"]]
    if not proposals:
        return {"tried": 0, "verified": 0}
    # Like ``seeds verify``: probing tries many slugs on few ATS hosts, so only the global budget bounds it.
    client = client_for_run(
        run, max_requests_per_host=settings["max_pages"], block_threshold=HOST_BLOCK_THRESHOLD,
        vendor_share=VERIFY_VENDOR_SHARE,
    )
    verified = []
    unconfirmed_seeds = []
    tried = 0
    page_hops = seeds.PageHops()
    deferred = []
    for proposal in proposals:
        tried += 1
        result = seeds.verify_proposal(
            proposal,
            client=client, timeout=settings["ats_api_timeout"], page_hops=page_hops,
        )
        if result.status == "verified" and result.seed:
            verified.append(seeds.as_feed_company(result.seed))
        elif (unconfirmed := seeds.stored_seed(result)) is not None:
            unconfirmed_seeds.append(unconfirmed)  # tagged guessed board, low priority (seed_yield decides)
        if result.reason == "budget_exhausted":
            deferred = [item.company for item in distinct[tried - 1:]]
            break
    added = 0
    if verified or unconfirmed_seeds:
        try:
            added = seeds.add_seeds([*verified, *unconfirmed_seeds], profile_slug=run["profile_slug"])
        except seeds.SeedFileError:
            added = 0
    return {"tried": tried, "verified": len(verified), "unconfirmed": len(unconfirmed_seeds),
            "seeds_added": added, "deferred": deferred}


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
