"""ATS boards for the toolkit: list a board's postings that match a profile, and find a company's board fast.

A board whose API returns each posting's text (Ashby, Lever) yields ``open`` dossiers at once, read and hinted;
other boards yield ``listed`` dossiers (open on the board, not read yet). A company's board is looked up in the
seed inventory first (no request), then probed on the vendors with a JSON board API only, stopping at the first
board that lists postings; no HTML is scraped.
"""

from __future__ import annotations

from typing import Any

from ultron.core.ats.clients import AtsClientError, fetch_vendor_jobs
from ultron.core.ats.clients.base import AtsPosting
from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.models import AtsBoardSeed, ExtractedJob
from ultron.infra.net import HostBlocked, HttpError, RequestBudgetExceeded, URLRejected, VendorBlocked
from ultron.seeds import load_inventory
from ultron.seeds.inventory import is_unconfirmed, make_seed_id
from ultron.seeds.naming import company_key, slug_candidates
from ultron.seeds.store import SeedFileError, add_seeds
from ultron.seeds.verify import Proposal, guessed_board_ownership
from ultron.stages.candidates import candidate_identity
from ultron.stages.discover import clean_posting, posting_candidate, seed_key
from ultron.stages.watch import profile_matches
from ultron.toolkit.dossier import ProfileContext, listed_dossier, posting_dossier

MAX_LISTED_PER_BOARD = 25
# Vendors whose board API returns each posting's text, so one board request reads all its postings.
TEXT_VENDORS = frozenset({"ashby", "lever", "greenhouse"})
# Vendors whose public board API answers in one JSON request (no HTML fallback), in probing order.
FAST_VENDORS = ("ashby", "greenhouse", "lever", "workable", "smartrecruiters", "recruitee", "teamtailor", "personio")
SLUGS_PER_COMPANY = 3
NETWORK_ERRORS = (AtsClientError, HostBlocked, HttpError, URLRejected, RequestBudgetExceeded, VendorBlocked)
PAYLOAD_ERRORS = (ValueError, KeyError, TypeError, AttributeError)


def cache_row(identity: str, status: str, candidate, job: dict, url: str) -> dict:
    """One inspect cache row: the posting's identity, the URL it was read at, its board and its facts."""
    return {"identity_key": identity, "status": status, "url": url, "source_type": candidate.source_type,
            "source_key": candidate.source_key if candidate.source_type == "ats_api" else "", "job": job}


def board_job(seed: AtsBoardSeed, posting: AtsPosting) -> ExtractedJob:
    """The posting a board API returned with its text, as an extracted job."""
    url = normalize_url(posting.apply_url)
    return ExtractedJob(apply_url=url, domain=domain_of(url), title=posting.title, company=seed.company,
                        location=posting.location, remote_text=posting.remote_hint, body_text=posting.description,
                        apply_available=True, posting_date=posting.posted_at)


def list_board(seed: AtsBoardSeed, client, timeout: int) -> list[AtsPosting]:
    """Every posting a board lists (raises the network errors)."""
    return [clean_posting(p) for p in fetch_vendor_jobs(seed.ats, seed.board_identifier, client=client,
                                                        timeout=timeout, include_content=True)]


def board_dossiers(seed: AtsBoardSeed, postings: list[AtsPosting], context: ProfileContext, *,
                   detail: bool = False, skip: set[str] = frozenset(),
                   keep_all: bool = False, limit: int | None = MAX_LISTED_PER_BOARD) -> tuple[list[dict], list[dict]]:
    """``(dossiers, cache rows)`` of a board's postings that match the profile, leaving out ``skip`` identities.

    With ``keep_all`` every posting of the board comes back (so a requested posting is found even when its title
    does not match the profile); those that do not match carry ``matches: False``.
    """
    dossiers, cache = [], []
    matching = profile_matches(postings, context.profile)
    chosen = postings if keep_all else matching[:limit]
    matched = {id(posting) for posting in matching}
    for posting in chosen:
        candidate = posting_candidate(seed, posting)
        if candidate is None:
            continue
        identity = candidate_identity(candidate.model_dump())
        if identity in skip:
            continue
        if posting.description:
            job = board_job(seed, posting)
            dossier = posting_dossier(job, identity, "open", context, detail=detail)
            dossier["board"] = seed_key(seed)
            cache.append(cache_row(identity, "open", candidate, job.model_dump(mode="json"), candidate.normalized_url))
        else:
            dossier = listed_dossier(posting, identity, seed.company, seed_key(seed))
            job = {"apply_url": candidate.normalized_url, "title": posting.title, "company": seed.company,
                   "location": posting.location}
            cache.append(cache_row(identity, "listed", candidate, job, candidate.normalized_url))
        if keep_all and id(posting) not in matched:
            dossier["matches"] = False
        dossiers.append(dossier)
    return dossiers, cache


def remember_board(seed: AtsBoardSeed, profile_slug: str) -> None:
    """Add a board proven to be the company's to the profile's seed inventory (best effort)."""
    try:
        add_seeds([seed.model_copy(update={"profiles": [profile_slug], "source_type": "toolkit_company_lookup"})],
                  profile_slug=profile_slug)
    except (OSError, ValueError, SeedFileError):
        return


def inventory_board(name: str) -> AtsBoardSeed | None:
    """The stored seed of a company with this name (no request), else ``None``."""
    wanted = company_key(name)
    if not wanted:
        return None
    return next((seed for seed in load_inventory(None) if company_key(seed.company) == wanted), None)


def probe_board(name: str, client, timeout: int) -> tuple[AtsBoardSeed | None, list[AtsPosting], bool]:
    """``(seed, postings, confirmed)`` of the first fast-vendor board of ``name`` that lists postings.

    A board the vendor reports under another company is skipped; one whose owner cannot be proven is kept only
    when no proven board answers, with ``confirmed`` false.
    """
    proposal = Proposal(company=name)
    fallback: tuple[AtsBoardSeed, list[AtsPosting]] | None = None
    for slug in slug_candidates(name, "")[:SLUGS_PER_COMPANY]:
        for vendor in FAST_VENDORS:
            seed = AtsBoardSeed(seed_id=make_seed_id(vendor, slug), company=name, ats=vendor, board_identifier=slug)
            try:
                postings = list_board(seed, client, timeout)
            except NETWORK_ERRORS + PAYLOAD_ERRORS:
                continue
            if not postings:
                continue
            ownership = guessed_board_ownership(proposal, slug, postings)
            if ownership == "owned":
                return seed, postings, True
            if ownership == "ownership_unknown" and fallback is None:
                fallback = (seed, postings)
    if fallback is not None:
        return fallback[0], fallback[1], False
    return None, [], False


def company_dossiers(name: str, client, timeout: int, context: ProfileContext, *,
                     detail: bool = False) -> tuple[list[dict], list[dict], dict[str, Any] | None]:
    """``(dossiers, cache rows, miss)``: a company's matching postings, or a miss record when it has none."""
    seed = inventory_board(name)
    confirmed = seed is not None and not is_unconfirmed(seed)
    postings: list[AtsPosting] = []
    if seed is not None:
        try:
            postings = list_board(seed, client, timeout)
        except NETWORK_ERRORS + PAYLOAD_ERRORS:
            seed = None
    if seed is None:
        seed, postings, confirmed = probe_board(name, client, timeout)
    if seed is None:
        return [], [], {"company": name, "status": "no_board"}
    if confirmed and context.profile_slug:
        remember_board(seed, context.profile_slug)
    dossiers, cache = board_dossiers(seed, postings, context, detail=detail)
    if not dossiers:
        return [], [], {"company": name, "status": "no_match", "board": seed_key(seed), "listed": len(postings)}
    if not confirmed:
        for dossier in dossiers:
            dossier["board_confirmed"] = False
    return dossiers, cache, None
