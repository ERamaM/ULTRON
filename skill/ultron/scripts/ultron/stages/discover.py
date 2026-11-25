"""``discover ats``: query ATS board APIs for the run's seed inventory."""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass, replace
from math import ceil
from urllib.parse import urlparse

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
from ultron.core.models import CandidateUrl, GeoScope
from ultron.core.remote_requirement import is_remote_required
from ultron.core.rules.geography import geo_scope, location_scope
from ultron.core.rules.term_matching import contains_term, normalize_text
from ultron.core.sectors import profile_sector_values
from ultron.core.text_clean import clean_text
from ultron.core.vocabulary import active_pack
from ultron.infra import home
from ultron.infra.net import HostBlocked, HttpError, RequestBudgetExceeded, URLRejected, VendorBlocked, top_hosts
from ultron.seeds import load_inventory
from ultron.stages import scout
from ultron.stages.candidates import candidate_identity as _identity
from ultron.stages.http_factory import DISCOVER_VENDOR_SHARE, HOST_BLOCK_THRESHOLD, VERIFY_VENDOR_SHARE, client_for_run
from ultron.storage import ledger, runs

# Seed groups, in query order. ``other`` = profile-linked auto seeds that are neither agent nor feed seeds
# (manual, imported). Pack seeds outside the profile's sectors or regions are never queried.
GROUPS = ("agent", "pack", "other", "feed")
FEED_SOURCE_TYPES = {"feed_discovered", "feed_company"}
NON_FEED_RESERVE = 0.5
# Share of seed slots kept for seeds without enough history, so new boards still get explored (at least 1 when capped).
EXPLORATION_RESERVE = 0.1
# Seeds this run requested (failures included, budget skips excluded); finalize turns them into yield rows.
SEED_ATTEMPTS_FILE = "seed_attempts.jsonl"
# Per queried board: postings the vendor listed, those that matched the profile, and those the per-board cap cut.
BOARD_REPORT_FILE = "board_report.jsonl"
# MARK III ``_seed_request_host``: the host group protected by ``max_requests_per_host``.
_VENDOR_HOSTS = {
    "greenhouse": "boards.greenhouse.io",
    "lever": "jobs.lever.co",
    "workable": "apply.workable.com",
    "smartrecruiters": "jobs.smartrecruiters.com",
    "occupop": "api.occupop.com",
    "candidatemanager": "www.candidatemanager.net",
}


def _seed_request_host(seed) -> str:
    vendor = str(seed.ats or "").strip().lower()
    if vendor == "ashby":
        return ASHBY_API_HOST
    board = str(seed.board_identifier or "").strip()
    if vendor == "hirehive":  # every board is its own tenant host (<board>.hirehive.com)
        return f"{board.casefold()}.hirehive.com"
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
        """Return True when a seed has been tried in at least three runs."""
        return self.attempts >= 3

    @property
    def deprioritized(self) -> bool:
        """Return True when a seed yielded zero keeps across five or more runs."""
        return self.attempts >= 5 and self.keeps == 0

    def rank(self) -> tuple[int, float]:
        """Seeds with keeps by keep ratio, then unmeasured seeds, then measured seeds without keeps."""
        if self.keeps > 0:  # even before 3 attempts: a seed that already produced keeps beats an unused one
            return 0, -self.keeps / self.attempts
        if not self.measured:
            return 1, 0.0
        return 2, 0.0


def seed_key(seed) -> str:
    """Return the canonical ``<vendor>:<identifier>`` key for a board seed."""
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


# Pack seed scopes beyond the legacy ``EU``, and the words of a profile's preferred regions or eligibility terms
# that select them. A scoped pack never competes for the seed slots of a profile in another region.
SCOPE_REGION_TERMS: dict[str, tuple[str, ...]] = {
    "LATAM": (
        "latam", "latin america", "latinoamerica", "south america", "central america", "americas", "brazil", "brasil",
        "mexico", "argentina", "colombia", "chile", "peru", "uruguay", "paraguay", "bolivia", "ecuador", "venezuela",
        "costa rica", "panama", "guatemala", "honduras", "el salvador", "nicaragua", "dominican republic",
        "puerto rico", "sao paulo", "rio de janeiro", "buenos aires", "bogota", "medellin", "cali", "mexico city",
        "ciudad de mexico", "cdmx", "guadalajara", "monterrey", "puebla", "queretaro", "tijuana", "lima",
        "montevideo", "quito", "curitiba", "belo horizonte", "porto alegre", "recife", "brasilia", "fortaleza",
        "florianopolis", "rosario", "mendoza", "barranquilla", "valparaiso", "asuncion", "santo domingo",
        "san salvador", "tegucigalpa", "managua", "guatemala city",
    ),
    "IE": (
        "ireland", "irish", "dublin", "cork", "galway", "limerick", "waterford", "kilkenny", "sligo", "drogheda",
        "dundalk", "athlone", "navan", "ennis", "tralee", "letterkenny", "wexford", "carlow", "kildare", "wicklow",
        "meath", "westmeath", "louth", "kerry", "mayo", "donegal", "tipperary", "cavan", "monaghan", "offaly",
        "laois", "roscommon", "leitrim", "longford",
    ),
    # States and major cities. Left out on purpose: "Georgia" (also a country), a bare "Washington" (only
    # "Washington DC"/"Washington State"), and cities that share their name with a city elsewhere ("San José",
    # "Portland", "Columbus", "Panama City").
    "US": (
        "united states", "usa", "us", "u.s.", "north america",
        "alabama", "alaska", "arizona", "arkansas", "california", "colorado", "connecticut", "delaware", "florida",
        "hawaii", "idaho", "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana", "maine", "maryland",
        "massachusetts", "michigan", "minnesota", "mississippi", "missouri", "montana", "nebraska", "nevada",
        "new hampshire", "new jersey", "new mexico", "new york", "north carolina", "north dakota", "ohio",
        "oklahoma", "oregon", "pennsylvania", "rhode island", "south carolina", "south dakota", "tennessee", "texas",
        "utah", "vermont", "virginia", "west virginia", "wisconsin", "wyoming", "washington dc", "washington d.c.",
        "washington state", "district of columbia",
        "new york city", "los angeles", "chicago", "houston", "phoenix", "philadelphia", "san antonio",
        "san diego", "dallas", "austin", "san francisco", "seattle", "boston", "atlanta", "miami", "denver",
        "nashville", "detroit", "minneapolis", "baltimore", "pittsburgh", "orlando", "tampa", "las vegas",
        "salt lake city", "kansas city", "st. louis", "saint louis", "raleigh", "sacramento", "indianapolis",
        "jacksonville", "memphis", "milwaukee", "oklahoma city", "albuquerque", "tucson", "honolulu",
        "new orleans", "charlotte nc",
    ),
}
# Phrases removed from the profile text before matching a scope: "New Mexico" is a US state, not Mexico.
SCOPE_EXCLUDED_PHRASES: dict[str, tuple[str, ...]] = {"LATAM": ("new mexico",)}
# US state codes after a comma ("Austin, TX"), only those that are not also ISO country codes ("IN" is India,
# "CA" Canada, "DE" Germany), so "Bangalore, IN" never reads as Indiana.
_US_STATE_CODE = re.compile(
    r",\s*(?:AK|CT|FL|HI|IA|KS|MI|NH|NJ|NM|NY|ND|OH|OK|OR|RI|TX|UT|VT|WA|WV|WI|WY|DC)\b"
)
# Scopes that fit every profile.
GLOBAL_SCOPES = frozenset({"GLOBAL", "WORLDWIDE"})
# ``_rank`` order of a pack seed that does not fit the profile (another sector or another region): it is left
# out of the selection entirely, never queried as filler. A profile without a matching pack relies on the seeds
# the agent generates for it (``ultron seeds plan``).
EXCLUDED_RANK = 5


def _profile_region_text(profile: dict | None) -> str:
    scope = location_scope(profile)
    return " | ".join((*scope["preferred"], *scope["eligibility_terms"]))


def seed_scope_fits(scope: str, profile: dict | None, *, geo: GeoScope | None = None,
                    region_text: str | None = None) -> bool:
    """Whether a pack seed's ``country_scope`` fits the profile's regions.

    ``EU`` (also a missing scope) fits European and region-free profiles, as before. ``GLOBAL`` fits every
    profile. A scope from ``SCOPE_REGION_TERMS`` fits a region-free profile, or one whose preferred regions or
    eligibility terms name it (a country, a city or the region itself). Any other scope fits every profile.
    """
    scope = str(scope or "").strip().upper() or "EU"
    geo = geo or geo_scope(profile)
    if scope == "EU":
        return geo != "other"
    if scope in GLOBAL_SCOPES or geo == "global":
        return True
    terms = SCOPE_REGION_TERMS.get(scope)
    if terms is None:
        return True
    text = _profile_region_text(profile) if region_text is None else region_text
    if scope == "US" and _US_STATE_CODE.search(text):
        return True
    for phrase in SCOPE_EXCLUDED_PHRASES.get(scope, ()):
        text = re.sub(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", " ", normalize_text(text))
    return any(contains_term(text, term) for term in terms)


def _rank(seed, profile_slug: str, sectors: set[str], geo: GeoScope = "eu", region_text: str = "") -> tuple[int, str]:
    """``(order, group)``: agent → pack matching sectors & region → other auto → feed; other packs are excluded.

    A pack seed fits when the profile declares no sector or the seed has none, or they share one (free-text
    profile sectors outside the vocabulary share none), and when its ``country_scope`` fits the profile.
    """
    if profile_slug in seed.profiles:
        if seed.source_type == "agent_generated":
            return 0, "agent"
        if seed.source_type in FEED_SOURCE_TYPES:
            return 3, "feed"
        return 2, "other"
    sector_match = not sectors or not seed.sectors or bool(sectors.intersection(seed.sectors))
    scope = str(getattr(seed, "country_scope", "") or "").strip().upper()
    if sector_match and seed_scope_fits(scope, None, geo=geo, region_text=region_text):
        return 1, "pack"
    return EXCLUDED_RANK, "excluded"


# Fewer seeds than this that fit the profile make a run a cold start: the skill then proposes more companies
# and runs more ATS ``site:`` searches (MARK II cold-start mode).
COLD_START_SEEDS = 15


def eligible_seed_count(profile_slug: str, profile: dict) -> int:
    """Inventory seeds that fit the profile: its linked auto seeds plus packs matching its sectors and region."""
    sectors = profile_sector_values((profile.get("criteria") or {}).get("sectors"))
    geo = geo_scope(profile)
    region_text = _profile_region_text(profile)
    return sum(
        1 for seed in load_inventory(profile_slug) if _rank(seed, profile_slug, sectors, geo, region_text)[0] < EXCLUDED_RANK
    )


def _select_seeds(
    profile_slug: str, profile_sectors, limit: int, per_host: int, *, geo: GeoScope = "eu",
    yields: dict[str, tuple[int, int]] | None = None, profile: dict | None = None,
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
    sectors = profile_sector_values(profile_sectors)
    region_text = _profile_region_text(profile)
    history = yields or {}

    def stat(seed) -> SeedYield:
        attempts, keeps = history.get(seed_key(seed), (0, 0))
        return SeedYield(max(0, int(attempts)), max(0, int(keeps or 0)))

    ranked_all = ((seed, *_rank(seed, profile_slug, sectors, geo, region_text)) for seed in load_inventory(profile_slug))
    ranked = sorted(
        (item for item in ranked_all if item[1] < EXCLUDED_RANK),
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


def clean_posting(posting):
    """``posting`` with its title and location cleaned of markup and stray whitespace."""
    return replace(posting, title=clean_text(posting.title), location=clean_text(posting.location))


def posting_candidate(seed, posting, *, search_pass: str = "") -> CandidateUrl | None:
    """The candidate row of a board posting (``None`` without an apply URL); ``search_pass`` tags its origin."""
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
        search_pass=search_pass,
    )


def _verify_search_proposals(
    run: dict, out_dir, *, timeout: int
) -> tuple[int, int, dict[str, list], dict[str, int], dict[str, int]]:
    """Verify new ATS boards from ``seed_proposals_from_search.jsonl``, save them, and cache their postings.

    The last two items are the verification client's requests per host and per vendor.
    """
    rows = runs.read_jsonl(out_dir / seed_store.SEARCH_PROPOSALS_FILE)
    if not rows:
        return 0, 0, {}, {}, {}
    verify_client = client_for_run(run, block_threshold=HOST_BLOCK_THRESHOLD, vendor_share=VERIFY_VENDOR_SHARE)
    known = {seed_identity(s.ats, s.board_identifier) for s in load_inventory(run["profile_slug"])}
    keys = ("company", "domain", "ats", "board", "careers_url", "source_url")
    limit = run["settings"]["new_companies_per_run"]
    verified = []
    prefetched: dict[str, list] = {}
    unconfirmed_seeds = []
    tried = 0
    page_hops = seed_store.PageHops()
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
            client=verify_client, timeout=timeout, verify_only=True, page_hops=page_hops,
        )
        if result.status == "verified" and result.seed:
            known.add(seed_identity(result.seed.ats, result.seed.board_identifier))
            verified.append(result.seed)
            # Workday verification only samples two pages. Query the board with the
            # discovery budget and location preferences, just like an existing seed.
            if result.seed.ats != "workday":
                prefetched[result.seed.seed_id] = [clean_posting(p) for p in result.jobs]
        elif (unconfirmed := seed_store.stored_seed(result)) is not None:
            # A guessed board not proven to be the proposal's: stored tagged at low priority (seed_yield decides).
            known.add(seed_identity(unconfirmed.ats, unconfirmed.board_identifier))
            unconfirmed_seeds.append(unconfirmed)
            if result.reason == "budget_exhausted":
                break
        elif result.reason == "budget_exhausted":
            break
    added = 0
    if verified or unconfirmed_seeds:
        try:
            added = seed_store.add_seeds([*verified, *unconfirmed_seeds], profile_slug=run["profile_slug"])
        except seed_store.SeedFileError:
            added = 0
    return len(verified), added, prefetched, verify_client.requests_by_host(), verify_client.requests_by_vendor()


def _merge_counts(*counts: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for count in counts:
        for key, value in count.items():
            merged[key] = merged.get(key, 0) + value
    return dict(sorted(merged.items()))


def discover_ats(run_id: str) -> dict:
    """Query ATS board APIs for active profile seeds and record candidate URLs."""
    run = runs.load_run(run_id)
    settings = run["settings"]
    out_dir = runs.run_dir(run_id)
    profile = run.get("profile_snapshot") or {}
    criteria = profile.get("criteria") or {}
    pack = active_pack(profile)
    geo = geo_scope(profile)
    scope = location_scope(profile)
    remote_required = is_remote_required(criteria)
    title_vocabulary = build_title_vocabulary(criteria, vocabulary=pack)
    per_seed = settings["ats_api_max_jobs_per_seed"]
    timeout = settings["ats_api_timeout"]
    search_verified, search_added, prefetched, verify_requests, verify_vendor_requests = _verify_search_proposals(
        run, out_dir, timeout=timeout,
    )
    client = client_for_run(run, block_threshold=HOST_BLOCK_THRESHOLD, vendor_share=DISCOVER_VENDOR_SHARE)
    scout_run = runs.run_mode(run) == "scout"
    if scout_run:  # found employers and the watchlist, never a sweep of the inventory
        seeds, groups = scout.expansion_seeds(run, out_dir, _seed_request_host)
        yield_stats = {"deprioritized": 0, "exploration_reserved": 0}
    else:
        seeds, groups, yield_stats = _select_seeds(
            run["profile_slug"], criteria.get("sectors"),
            settings["max_seeds_per_run"], settings["max_requests_per_host"], geo=geo,
            yields=load_seed_yields(run["profile_slug"]), profile=profile,
        )
    board_report: list[dict] = []

    candidates: list[CandidateUrl] = []
    seen_urls: set[str] = set()
    errors: list[dict] = []
    queried = 0
    budget_spent = False
    # Workday boards paginate: each one gets an equal share of what the vendor budget leaves for this stage.
    workday_left = sum(1 for seed, _group in seeds if seed.ats == "workday" and seed.seed_id not in prefetched)

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
                options = {}
                if seed.ats == "workday":
                    options["preferred_locations"] = scope["preferred"]
                    allowance = client.vendor_allowance("workday")
                    if allowance is not None:
                        options["max_pages"] = max(1, allowance // max(1, workday_left))
                    workday_left -= 1
                postings = [clean_posting(p) for p in fetch_vendor_jobs(
                    seed.ats, seed.board_identifier, client=client, timeout=timeout, **options)]
            except RequestBudgetExceeded as exc:
                if exc.scope == "host":
                    errors.append({
                        "seed_id": seed.seed_id, "error": "host_budget_exhausted", "host": exc.host, "skipped": True,
                    })
                elif exc.scope == "vendor":
                    errors.append({
                        "seed_id": seed.seed_id, "error": "vendor_budget_exhausted", "vendor": exc.vendor,
                        "skipped": True,
                    })
                else:
                    budget_spent = True
                    errors.append({"seed_id": seed.seed_id, "error": "request_budget_exhausted", "skipped": True})
                continue
            except AtsClientError as exc:
                errors.append({"seed_id": seed.seed_id, "error": str(exc)})
                continue
            except VendorBlocked as exc:
                # Hosts of this vendor (any tenant) answered 429/403 too often in this run: no request was sent.
                errors.append({"seed_id": seed.seed_id, "error": "vendor_blocked", "vendor": exc.vendor,
                               "host": exc.host, "skipped": True})
                continue
            except HostBlocked as exc:
                # The host kept answering 429/403: no request was sent for this board.
                errors.append({"seed_id": seed.seed_id, "error": "host_blocked", "host": str(exc).split(": ", 1)[-1],
                               "skipped": True})
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
        board_report.append({"seed_key": seed_key(seed), "company": seed.company, "group": _group,
                             "listed": len(postings), "matched": len(kept), "cut": max(0, len(kept) - per_seed)})
        for posting in kept[:per_seed]:
            candidate = posting_candidate(seed, posting, search_pass=scout.EXPANSION_PASS if scout_run else "")
            if candidate is None or candidate.normalized_url in seen_urls:
                continue
            seen_urls.add(candidate.normalized_url)
            candidates.append(candidate)

    # Keep candidates added by other stages (web search, feeds); ATS rows are rebuilt on every run.
    # A feed row for a posting this run also found through its ATS board yields to the ATS row.
    previous = runs.read_jsonl(out_dir / "candidates.jsonl")
    if scout_run:
        # Agent finds and watched postings keep their place and provenance; expansion only adds postings the
        # run does not have yet (an earlier expansion is rebuilt).
        kept_rows = [r for r in previous if r.get("search_pass") != scout.EXPANSION_PASS]
        known = {_identity(r) for r in kept_rows}
        merged = [*kept_rows, *(c for c in candidates if _identity(c.model_dump()) not in known)]
    else:
        ats_identities = {_identity(c.model_dump()) for c in candidates}
        kept_rows = [
            r for r in previous
            if r.get("source_type") != "ats_api"
            and not (str(r.get("source_type") or "").startswith("feed:") and _identity(r) in ats_identities)
        ]
        merged = [*candidates, *kept_rows]
    runs.write_jsonl(out_dir / "candidates.jsonl", merged)
    runs.write_jsonl(out_dir / "seed_errors.jsonl", errors)
    runs.write_jsonl(out_dir / BOARD_REPORT_FILE, board_report)
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
        "mode": "scout" if scout_run else "sweep",
        "seeds_queried": queried,
        "board_postings": {"listed": sum(b["listed"] for b in board_report),
                           "matched": sum(b["matched"] for b in board_report),
                           "cut_by_cap": sum(b["cut"] for b in board_report)},
        "seeds_failed": len(errors) - skipped,
        "seeds_skipped": skipped,
        "candidates": len(candidates),
        "requests": client.requests_made + sum(verify_requests.values()),
        "requests_by_domain": top_hosts(client.requests_by_host(), verify_requests),
        "requests_by_vendor": _merge_counts(client.requests_by_vendor(), verify_vendor_requests),
        "vendors_blocked": client.vendors_blocked(),
        "seed_groups": groups,
        "seeds_deprioritized": yield_stats["deprioritized"],
        "seeds_exploration_reserved": yield_stats["exploration_reserved"],
        "search_proposals_verified": search_verified,
        "search_proposals_added": search_added,
    }
