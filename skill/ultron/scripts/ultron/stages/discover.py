"""``discover ats``: query ATS board APIs for the run's seed inventory."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from math import ceil
from urllib.parse import urlparse

from ultron import home, ledger, runs
from ultron import seeds as seed_store
from ultron.core.ats.clients import AtsClientError, fetch_vendor_jobs
from ultron.core.ats.clients.ashby import ASHBY_API_HOST
from ultron.core.ats.identity import seed_identity
from ultron.core.discovery.prefilter import (
    is_clearly_ineligible_at_discovery,
    passes_coarse_prefilter,
    posting_rank_score,
)
from ultron.core.discovery.title_vocabulary import build_title_vocabulary
from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import CandidateUrl, GeoScope
from ultron.core.rules.geography import geo_scope, location_scope
from ultron.core.text_clean import clean_text
from ultron.core.vocabulary import active_pack
from ultron.http_factory import client_for_run
from ultron.net import HttpError, RequestBudgetExceeded, URLRejected
from ultron.seeds import load_inventory


def _identity(candidate: dict) -> str:
    return canonical_job_identity(
        candidate.get("source_url") or candidate.get("normalized_url") or "",
        source_type=candidate.get("source_type") or "",
        source_key=candidate.get("source_key") or "",
    ).identity_key


# Seed groups, in query order. ``other`` = profile-linked auto seeds that are neither agent nor feed seeds
# (manual, imported) plus pack seeds outside the profile's sectors, which rank last.
GROUPS = ("agent", "pack", "other", "feed")
FEED_SOURCE_TYPES = {"feed_discovered", "feed_company"}
NON_FEED_RESERVE = 0.5
# Share of seed slots kept for seeds without enough history, so new boards still get explored (at least 1 when capped).
EXPLORATION_RESERVE = 0.1
# Seeds this run requested (failures included, budget skips excluded); finalize turns them into yield rows.
SEED_ATTEMPTS_FILE = "seed_attempts.jsonl"
# MARK III ``_seed_request_host``: the host group protected by ``max_requests_per_host``.
_VENDOR_HOSTS = {
    "greenhouse": "boards.greenhouse.io",
    "lever": "jobs.lever.co",
    "workable": "apply.workable.com",
    "smartrecruiters": "jobs.smartrecruiters.com",
}


def _seed_request_host(seed) -> str:
    vendor = str(seed.ats or "").strip().lower()
    if vendor == "ashby":
        return ASHBY_API_HOST
    board = str(seed.board_identifier or "").strip()
    if "://" in board or "/" in board or "." in board:
        parsed = urlparse(board if "://" in board else f"//{board}")
        if parsed.hostname:
            return parsed.hostname.casefold().rstrip(".")
    return _VENDOR_HOSTS.get(vendor, vendor or "unknown")


@dataclass(frozen=True, slots=True)
class SeedYield:
    """A seed's history for one profile: finalized runs that queried it, and runs that kept a posting from it."""

    attempts: int = 0
    keeps: int = 0

    @property
    def measured(self) -> bool:
        return self.attempts >= 3

    @property
    def deprioritized(self) -> bool:
        return self.attempts >= 5 and self.keeps == 0

    def rank(self) -> tuple[int, float]:
        """Seeds with keeps by keep ratio, then unmeasured seeds, then measured seeds without keeps."""
        if self.keeps > 0:  # even before 3 attempts: a seed that already produced keeps beats an unused one
            return 0, -self.keeps / self.attempts
        if not self.measured:
            return 1, 0.0
        return 2, 0.0


def seed_key(seed) -> str:
    return ":".join(seed_identity(seed.ats, seed.board_identifier))


def load_seed_yields(profile_slug: str) -> dict[str, tuple[int, int]]:
    """The profile's seed history from the ledger; empty (and no ledger created) before the first finalize."""
    if not (home.ultron_home() / "ultron.sqlite3").exists():
        return {}
    try:
        with closing(ledger.connect()) as conn:
            return ledger.seed_yields(conn, profile_slug)
    except sqlite3.Error:
        return {}


def _rank(seed, profile_slug: str, sectors: set[str], geo: GeoScope = "eu") -> tuple[int, str]:
    """``(order, group)``: agent → pack matching sectors & geo → other auto → feed → non-matching pack."""
    if profile_slug in seed.profiles:
        if seed.source_type == "agent_generated":
            return 0, "agent"
        if seed.source_type in FEED_SOURCE_TYPES:
            return 3, "feed"
        return 2, "other"
    sector_match = not sectors or not seed.sectors or bool(sectors.intersection(seed.sectors))
    seed_scope = str(getattr(seed, "country_scope", "") or "").strip().upper()
    geo_match = geo != "other" or seed_scope != "EU"
    return (1, "pack") if (sector_match and geo_match) else (4, "other")


def _select_seeds(
    profile_slug: str, profile_sectors, limit: int, per_host: int, *, geo: GeoScope = "eu",
    yields: dict[str, tuple[int, int]] | None = None,
) -> tuple[list[tuple[object, str]], dict[str, dict[str, int]], dict[str, int]]:
    """Curated seeds first, then ``limit`` with a non-feed reserve, then the MARK III per-host cap.

    Seeds whose history for this profile (``yields``: seed key -> ``(attempts, keeps)``) shows 5+ attempts
    without a kept posting go last, after every group. Within a group, seeds with keeps come first by keep
    ratio, then seeds with fewer than 3 attempts, then measured seeds without keeps; ties keep the
    zero-postings flag and ``priority`` order (stable). With no history this is the plain group/priority order.
    When eligible seeds exceed ``limit``, ``EXPLORATION_RESERVE`` of it (at least 1) is kept for unmeasured
    seeds: when the plain order would leave them out, the best-ranked ones are picked first (taking host and
    feed slots before the rest). Feed seeds may take at most ``limit`` minus the reserved share
    (``NON_FEED_RESERVE`` of ``limit``, or fewer when fewer non-feed seeds fit under the host cap). Host slots
    are counted only for seeds actually selected, so a seed dropped by the feed cap leaves its host slot to a
    later seed, and a host-capped seed does not use a seed slot. Returns the selected ``(seed, group)`` pairs
    in query order, per-group counts (``host_capped``, ``capped``) and yield stats
    (``deprioritized``, ``exploration_reserved``).
    """
    sectors = {str(s) for s in profile_sectors or ()}
    history = yields or {}

    def stat(seed) -> SeedYield:
        attempts, keeps = history.get(seed_key(seed), (0, 0))
        return SeedYield(max(0, int(attempts)), max(0, int(keeps or 0)))

    ranked = sorted(
        ((seed, *_rank(seed, profile_slug, sectors, geo)) for seed in load_inventory(profile_slug)),
        key=lambda item: (
            stat(item[0]).deprioritized,
            item[1],
            *stat(item[0]).rank(),
            1 if (item[0].verification or {}).get("postings") == 0 else 0,
            item[0].priority,
        ),
    )
    per_host = max(1, per_host)
    counts = {g: {"queried": 0, "failed": 0, "skipped": 0, "host_capped": 0, "capped": 0} for g in GROUPS}
    # Non-feed seeds that would fit under the host cap on their own: the size of the reserve.
    hosts: dict[str, int] = {}
    non_feed = 0
    for seed, _order, group in ranked:
        host = _seed_request_host(seed)
        if group != "feed" and hosts.get(host, 0) < per_host:
            hosts[host] = hosts.get(host, 0) + 1
            non_feed += 1
    feed_cap = limit - min(non_feed, ceil(limit * NON_FEED_RESERVE))
    unmeasured = [i for i, (seed, _order, _group) in enumerate(ranked) if not stat(seed).measured]
    reserve = min(max(1, ceil(limit * EXPLORATION_RESERVE)), len(unmeasured)) if len(ranked) > limit else 0

    def fill(explore: list[int], wanted: int) -> tuple[list[int], dict[int, str]]:
        """Pick up to ``wanted`` of ``explore`` first, then fill the remaining slots in ranked order."""
        hosts: dict[str, int] = {}
        chosen: list[int] = []
        reason: dict[int, str] = {}
        feeds = 0

        def consider(i: int) -> bool:
            nonlocal feeds
            seed, _order, group = ranked[i]
            host = _seed_request_host(seed)
            if hosts.get(host, 0) >= per_host:
                reason[i] = "host_capped"
                return False
            if len(chosen) >= limit or (group == "feed" and feeds >= feed_cap):
                reason[i] = "capped"
                return False
            hosts[host] = hosts.get(host, 0) + 1
            feeds += group == "feed"
            chosen.append(i)
            reason.pop(i, None)
            return True

        picked = 0
        for i in explore:
            if picked >= wanted:
                break
            picked += consider(i)
        taken = set(chosen)
        for i in range(len(ranked)):
            if i not in taken:
                consider(i)
        return chosen, reason

    chosen, reason = fill([], 0)
    plain = set(chosen)
    # Exploration reserve: when the plain order gives unmeasured seeds fewer slots than the reserve, pick the
    # best-ranked unmeasured seeds first (they take host and feed slots before the rest), then refill.
    if reserve > len(plain.intersection(unmeasured)):
        chosen, reason = fill(unmeasured, reserve)
    reserved = len(set(chosen).intersection(unmeasured) - plain)
    for i, why in reason.items():
        counts[ranked[i][2]][why] += 1
    # Query in pick order: reserved exploration seeds first, so a higher-ranked board on the same host cannot
    # spend the shared host request budget before them; without a reserve this is the ranked order.
    selected = [(ranked[i][0], ranked[i][2]) for i in chosen]
    stats = {
        "deprioritized": sum(1 for seed, _order, _group in ranked if stat(seed).deprioritized),
        "exploration_reserved": reserved,
    }
    return selected, counts, stats


def _clean_posting(posting):
    return replace(posting, title=clean_text(posting.title), location=clean_text(posting.location))


def _candidate(seed, posting) -> CandidateUrl | None:
    url = str(posting.apply_url or "").strip()
    if not url:
        return None
    normalized = normalize_url(url)
    source_key = f"{seed.ats}:{seed.board_identifier}"
    snippet = " | ".join(part for part in (posting.location, posting.remote_hint) if part).strip()
    return CandidateUrl(
        query_id=source_key,
        query="",
        source_url=url,
        normalized_url=normalized,
        domain=domain_of(normalized),
        ats_title=posting.title,
        ats_location=posting.location,
        ats_remote_hint=posting.remote_hint,
        ats_company=clean_text(seed.company),
        source_type="ats_api",
        source_key=source_key,
        source_label=f"{seed.company} ({seed.ats})",
        hit_title=posting.title,
        snippet=snippet,
        matched_vendor=seed.ats,
    )


def _verify_search_proposals(run: dict, out_dir, *, timeout: int) -> tuple[int, int, dict[str, list], int]:
    """Verify new ATS boards from ``seed_proposals_from_search.jsonl``, save them, and cache their postings."""
    rows = runs.read_jsonl(out_dir / seed_store.SEARCH_PROPOSALS_FILE)
    if not rows:
        return 0, 0, {}, 0
    verify_client = client_for_run(run)
    known = {seed_identity(s.ats, s.board_identifier) for s in load_inventory(run["profile_slug"])}
    keys = ("company", "domain", "ats", "board", "careers_url", "source_url")
    limit = run["settings"]["new_companies_per_run"]
    verified = []
    prefetched: dict[str, list] = {}
    tried = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        ats = str(row.get("ats") or "").strip().lower()
        board = str(row.get("board") or "").strip()
        if ats and board and seed_identity(ats, board) in known:
            continue
        if tried >= limit:
            break
        tried += 1
        result = seed_store.verify_proposal(
            seed_store.Proposal(**{k: str(row.get(k) or "").strip() for k in keys}),
            client=verify_client, timeout=timeout, verify_only=True,
        )
        if result.status == "verified" and result.seed:
            known.add(seed_identity(result.seed.ats, result.seed.board_identifier))
            verified.append(result.seed)
            # Workday verification only samples two pages. Query the board with the
            # discovery budget and location preferences, just like an existing seed.
            if result.seed.ats != "workday":
                prefetched[result.seed.seed_id] = [_clean_posting(p) for p in result.jobs]
        elif result.reason == "budget_exhausted":
            break
    added = 0
    if verified:
        try:
            added = seed_store.add_seeds(verified, profile_slug=run["profile_slug"])
        except seed_store.SeedFileError:
            added = 0
    return len(verified), added, prefetched, verify_client.requests_made


def discover_ats(run_id: str) -> dict:
    run = runs.load_run(run_id)
    settings = run["settings"]
    out_dir = runs.run_dir(run_id)
    profile = run.get("profile_snapshot") or {}
    criteria = profile.get("criteria") or {}
    pack = active_pack(profile)
    geo = geo_scope(profile)
    scope = location_scope(profile)
    remote_req_val = str((criteria.get("remote_policy") or {}).get("required") or "full_remote").strip().lower()
    remote_required = remote_req_val not in {"any", "none", "optional", "hybrid", "onsite", "on-site", "false"}
    title_vocabulary = build_title_vocabulary(criteria, vocabulary=pack)
    per_seed = settings["ats_api_max_jobs_per_seed"]
    timeout = settings["ats_api_timeout"]
    search_verified, search_added, prefetched, verify_requests = _verify_search_proposals(run, out_dir, timeout=timeout)
    client = client_for_run(run)
    seeds, groups, yield_stats = _select_seeds(
        run["profile_slug"], criteria.get("sectors"),
        settings["max_seeds_per_run"], settings["max_requests_per_host"], geo=geo,
        yields=load_seed_yields(run["profile_slug"]),
    )

    candidates: list[CandidateUrl] = []
    seen_urls: set[str] = set()
    errors: list[dict] = []
    queried = 0
    budget_spent = False

    for seed, _group in seeds:
        if seed.seed_id in prefetched:
            queried += 1
            postings = prefetched[seed.seed_id]
        else:
            if budget_spent:
                errors.append({"seed_id": seed.seed_id, "error": "request_budget_exhausted", "skipped": True})
                continue
            queried += 1
            try:
                options = {"preferred_locations": scope["preferred"]} if seed.ats == "workday" else {}
                postings = [_clean_posting(p) for p in fetch_vendor_jobs(
                    seed.ats, seed.board_identifier, client=client, timeout=timeout, **options)]
            except RequestBudgetExceeded as exc:
                if exc.scope == "host":
                    errors.append({
                        "seed_id": seed.seed_id, "error": "host_budget_exhausted", "host": exc.host, "skipped": True,
                    })
                else:
                    budget_spent = True
                    errors.append({"seed_id": seed.seed_id, "error": "request_budget_exhausted", "skipped": True})
                continue
            except AtsClientError as exc:
                errors.append({"seed_id": seed.seed_id, "error": str(exc)})
                continue
            except HttpError as exc:
                errors.append({"seed_id": seed.seed_id, "error": f"http_error: {exc} (status={exc.status_code})"})
                continue
            except URLRejected as exc:
                errors.append({"seed_id": seed.seed_id, "error": f"url_rejected: {exc}"})
                continue
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                errors.append({"seed_id": seed.seed_id, "error": f"unexpected_payload: {exc!r}"})
                continue
        kept = [
            p for p in postings
            if passes_coarse_prefilter(p, title_vocabulary=title_vocabulary, vocabulary=pack)
            and not is_clearly_ineligible_at_discovery(
                p, geo=geo, remote_required=remote_required, excluded_location_terms=scope["excluded_terms"],
            )
        ]
        kept.sort(
            key=lambda p: posting_rank_score(
                p,
                title_vocabulary=title_vocabulary,
                vocabulary=pack,
                geo=geo,
                remote_required=remote_required,
                preferred_regions=scope["preferred"],
                eligibility_terms=scope["eligibility_terms"],
                excluded_location_terms=scope["excluded_terms"],
            ),
            reverse=True,
        )
        for posting in kept[:per_seed]:
            candidate = _candidate(seed, posting)
            if candidate is None or candidate.normalized_url in seen_urls:
                continue
            seen_urls.add(candidate.normalized_url)
            candidates.append(candidate)

    # Keep candidates added by other stages (web search, feeds); ATS rows are rebuilt on every run.
    # A feed row for a posting this run also found through its ATS board yields to the ATS row.
    ats_identities = {_identity(c.model_dump()) for c in candidates}
    kept_rows = [
        r for r in runs.read_jsonl(out_dir / "candidates.jsonl")
        if r.get("source_type") != "ats_api"
        and not (str(r.get("source_type") or "").startswith("feed:") and _identity(r) in ats_identities)
    ]
    runs.write_jsonl(out_dir / "candidates.jsonl", [*candidates, *kept_rows])
    runs.write_jsonl(out_dir / "seed_errors.jsonl", errors)
    skipped_ids = {e["seed_id"] for e in errors if e.get("skipped")}
    runs.write_jsonl(
        out_dir / SEED_ATTEMPTS_FILE,
        [{"seed_key": seed_key(seed)} for seed, _group in seeds if seed.seed_id not in skipped_ids],
    )
    runs.mark_stage(run_id, "discover_ats")
    skipped = sum(1 for e in errors if e.get("skipped"))
    # Per group: queried = selected and requested (failures included); skipped = selected but not requested.
    group_of = {seed.seed_id: group for seed, group in seeds}
    for _seed, group in seeds:
        groups[group]["queried"] += 1
    for error in errors:
        group = groups[group_of[error["seed_id"]]]
        if error.get("skipped"):
            group["skipped"] += 1
            group["queried"] -= 1
        else:
            group["failed"] += 1
    return {
        "seeds_queried": queried,
        "seeds_failed": len(errors) - skipped,
        "seeds_skipped": skipped,
        "candidates": len(candidates),
        "requests": client.requests_made + verify_requests,
        "seed_groups": groups,
        "seeds_deprioritized": yield_stats["deprioritized"],
        "seeds_exploration_reserved": yield_stats["exploration_reserved"],
        "search_proposals_verified": search_verified,
        "search_proposals_added": search_added,
    }
