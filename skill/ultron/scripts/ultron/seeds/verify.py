"""On-the-fly company verification: probe vendors, judge board ownership and build seeds."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlparse

from ultron.core.ats.clients import AtsClientError, fetch_vendor_jobs
from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, is_direct_supported_ats_host
from ultron.core.ats.vendors import SUPPORTED_ATS_DOMAINS
from ultron.core.feeds.urls import AGGREGATOR_DOMAINS, fetch_page_links, looks_like_company_jobs_url
from ultron.core.models import AtsBoardSeed
from ultron.infra.net import HostBlocked, HttpClient, HttpError, RequestBudgetExceeded, URLRejected
from ultron.seeds.inventory import (
    OTHER_COMPANY_SOURCE_TYPE,
    UNCONFIRMED_SEED_PRIORITY,
    UNCONFIRMED_SOURCE_TYPE,
    make_seed_id,
)
from ultron.seeds.naming import (
    ascii_fold,
    company_key,
    core_company_words,
    domain_slug,
    registrable_domain,
    slug_candidate_passes,
)

# Vendors probed by slug, in order. The bare-slug vendors added last (jobvite, bamboohr, jazzhr, factorial) cost
# about 2, 2, 1 and 2 requests per missed slug. The rest (workday, icims, taleo need a host; comeet needs a
# company UID next to the slug) are only reached via an explicit ats/board pair or a careers_url.
PROBE_VENDORS = (
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "teamtailor",
    "smartrecruiters",
    "personio",
    "pinpoint",
    "homerun",
    "breezy",
    "jobvite",
    "bamboohr",
    "jazzhr",
    "factorial",
)


@dataclass
class Proposal:
    """Company or career-site candidate proposed for ATS verification."""
    company: str
    domain: str = ""
    ats: str = ""
    board: str = ""
    careers_url: str = ""
    source_url: str = ""


@dataclass
class VerifyResult:
    """Outcome of verifying a seed ``Proposal`` against ATS probes."""
    proposal: Proposal
    status: Literal["verified", "unresolved"]
    seed: AtsBoardSeed | None
    postings: int
    reason: str
    jobs: tuple = field(default=(), repr=False, compare=False)
    # For ``ownership_unknown`` / ``wrong_tenant``: the guessed board that answered ({ats, board, postings}).
    candidate: dict | None = None


# Trailing legal-form words that never belong in an ATS board slug ("Acme GmbH" -> "acme").
# Only legal forms: product words such as "ai" or "labs" can name a different company's board.
def _vendor_company_names(jobs) -> set[str]:
    """Company names the vendor reports in its postings (Greenhouse/Recruitee ``company_name``,
    SmartRecruiters ``company.name``, ``companyName``), as company keys. Empty when it reports none."""
    names: set[str] = set()
    for job in list(jobs)[:20]:
        raw = getattr(job, "raw_payload", None)
        if not isinstance(raw, dict):
            continue
        company = raw.get("company")
        values = (
            raw.get("company_name"),
            raw.get("companyName"),
            company.get("name") if isinstance(company, dict) else company,
        )
        names.update(company_key(v) for v in values if isinstance(v, str) and v.strip())
    names.discard("")
    return names


# A name with one core word or a short key ("Acme", "Acme GmbH", "Nova") is shared by many companies: only a
# slug taken from the company's own domain ties such a board to it.
_SHORT_COMPANY_KEY = 6


def guessed_board_ownership(p: Proposal, board: str, jobs) -> Literal["owned", "wrong_tenant", "ownership_unknown"]:
    """Whether a board found by guessing ``board`` from the proposal belongs to the proposed company.

    ``owned`` when the slug is the proposal domain's registrable label, or when the name has two or more core
    words and the vendor reports the same company key. ``wrong_tenant`` when the vendor reports another company.
    ``ownership_unknown`` otherwise (a short or one-word name without a domain slug, or no vendor name).
    """
    slug = board.lower().replace("-", "")
    domain_label = domain_slug(p.domain).replace("-", "")
    if domain_label and slug == domain_label:
        return "owned"
    vendor_names = _vendor_company_names(jobs)
    if vendor_names and not vendor_names & _company_keys(p.company):
        return "wrong_tenant"
    if _is_short_name(p.company):
        return "ownership_unknown"
    return "owned" if vendor_names else "ownership_unknown"


def _is_short_name(company: str) -> bool:
    words = core_company_words(company)
    return len(words) < 2 or len("".join(words)) < _SHORT_COMPANY_KEY


def _vendor_company_display(jobs) -> str:
    """The first company name the vendor reports in its postings, as written ("" when none)."""
    for job in list(jobs)[:20]:
        raw = getattr(job, "raw_payload", None)
        if not isinstance(raw, dict):
            continue
        company = raw.get("company")
        for value in (raw.get("company_name"), raw.get("companyName"),
                      company.get("name") if isinstance(company, dict) else company):
            if isinstance(value, str) and value.strip() and company_key(value):
                return " ".join(value.split())
    return ""


# Guessed boards not proven to be the proposal's are still stored (their postings are real and every result's
# company comes from the posting), but last in their group: seed_yield then decides whether they stay useful.
def unconfirmed_seed(p: Proposal, ats: str, board: str, jobs, verdict: str) -> AtsBoardSeed | None:
    """Seed for a guessed board that answered without proof of ownership.

    ``wrong_tenant`` with a vendor-reported name: the board is stored under that company, tagged
    ``discovered_via:<proposal>``. Otherwise (``ownership_unknown``) it keeps the proposal's name, tagged
    ``ownership:unknown``. Both get ``UNCONFIRMED_SEED_PRIORITY``.
    """
    try:
        seed = _build_seed(p, ats, board, len(jobs))
    except ValueError:  # vendor outside the supported Literal
        return None
    vendor_name = _vendor_company_display(jobs) if verdict == "wrong_tenant" else ""
    if vendor_name:
        update = {
            "company": vendor_name,
            "tags": [f"discovered_via:{p.company}"],
            "source_type": OTHER_COMPANY_SOURCE_TYPE,
            "source_label": f"Guessed board of another company (via {p.company})",
        }
    else:
        update = {
            "tags": ["ownership:unknown"],
            "source_type": UNCONFIRMED_SOURCE_TYPE,
            "source_label": "Guessed board (ownership unknown)",
        }
    return seed.model_copy(update={**update, "priority": UNCONFIRMED_SEED_PRIORITY})


def stored_seed(result: VerifyResult) -> AtsBoardSeed | None:
    """The seed a verification stores: the verified board, or a tagged unconfirmed guessed board.

    A verification stopped by the request budget still stores the unconfirmed board it had already found.
    """
    if result.status == "verified" or result.reason in {"ownership_unknown", "wrong_tenant"}:
        return result.seed
    if result.reason == "budget_exhausted" and result.seed is not None:
        return result.seed
    return None


def _build_seed(p: Proposal, ats: str, board: str, postings: int) -> AtsBoardSeed:
    return AtsBoardSeed(
        seed_id=make_seed_id(ats, board),
        company=p.company,
        ats=ats,  # type: ignore[arg-type]
        board_identifier=board,
        source_type="agent_generated",
        source_label="Agent proposal",
        source_url=p.careers_url or p.source_url,
        verification={"postings": postings, "verified_at": datetime.now(UTC).isoformat()},
    )


FEED_PROPOSALS_FILE = "seed_proposals_from_feeds.jsonl"
SEARCH_PROPOSALS_FILE = "seed_proposals_from_search.jsonl"


def as_feed_company(seed: AtsBoardSeed) -> AtsBoardSeed:
    """Label a board verified from a feed company name (no agent proposed it)."""
    return seed.model_copy(update={"source_type": "feed_company", "source_label": "Feed company (verified)"})


# One capped hop from a company careers page (or an aggregator posting page) to the ATS boards it links
# (MARK II seed discovery expansion pass). Totals are per ``PageHops`` (one per ``seeds verify`` call or stage).
PAGE_HOP_TOTAL = 10
PAGE_HOP_PER_HOST = 2
PAGE_HOP_MAX_BOARDS = 3
_AGGREGATOR_REGISTRABLE = frozenset(d.removeprefix("www.") for d in AGGREGATOR_DOMAINS)


@dataclass
class PageHops:
    """Careers/aggregator page fetches left: at most ``total`` pages and ``per_host`` pages per host."""

    total: int = PAGE_HOP_TOTAL
    per_host: int = PAGE_HOP_PER_HOST
    used: dict[str, int] = field(default_factory=dict)

    def take(self, host: str) -> bool:
        """Consume one host and total page-hop budget slot, returning False if exhausted."""
        if sum(self.used.values()) >= self.total or self.used.get(host, 0) >= self.per_host:
            return False
        self.used[host] = self.used.get(host, 0) + 1
        return True


class _DomainScopedPages:
    """``fetch_page_links`` client whose requests and redirect hops stay on the page's registrable domain or a
    supported ATS domain (a careers page that redirects to its hosted board)."""

    def __init__(self, client: HttpClient, domain: str) -> None:
        self._client = client
        self._domains = (domain, *SUPPORTED_ATS_DOMAINS)

    def get_text(self, url: str) -> tuple[int, str, str]:
        return self._client.get_text(url, allowed_domains=self._domains)


def _page_hop_target(p: Proposal, *, explicit: bool) -> tuple[str, str, str] | None:
    """``(url, registrable domain, kind)`` of the page to follow, kind ``company`` or ``aggregator``.

    With an explicit proposal only a ``careers_url`` that is not an ATS URL qualifies; otherwise the
    ``source_url`` (a feed posting page) does. A page is the company's own when it sits on the proposal's
    domain or, without a domain, when its domain label is the company key. Aggregator pages, and an agent's
    careers-looking ``careers_url`` on a domain that does not name the company, are judged as aggregators.
    """
    url = (p.careers_url if explicit else p.source_url).strip()
    if not url or urlparse(url).scheme not in {"http", "https"} or extract_seed_from_url(url, "") is not None:
        return None
    reg = registrable_domain(url)
    if not reg or is_direct_supported_ats_host(reg):
        return None
    if reg in _AGGREGATOR_REGISTRABLE:
        return url, reg, "aggregator"
    own = registrable_domain(p.domain)
    if own:
        return (url, reg, "company") if reg == own else None
    if domain_slug(reg).replace("-", "") in _company_keys(p.company):
        return url, reg, "company"
    if explicit and looks_like_company_jobs_url(url):
        return url, reg, "aggregator"
    return None


def _company_keys(company: str) -> set[str]:
    """The company key with and without legal forms ("acme", "acmegmbh" for "Acme GmbH")."""
    return {company_key(company), "".join(re.findall(r"[a-z0-9]+", ascii_fold(company)))} - {""}


def _aggregator_board_is_company(p: Proposal, board: str, jobs) -> bool:
    """A page that does not belong to the company links other companies' boards too: accept a board whose slug
    is the company key or whose vendor reports the company's name, never one whose vendor names another."""
    keys = _company_keys(p.company)
    vendor_names = _vendor_company_names(jobs)
    if vendor_names:
        return bool(vendor_names & keys)
    return board.lower().replace("-", "") in keys


def _linked_boards(links: list[str]) -> list[tuple[str, str]]:
    """Distinct ATS boards linked from a page: only links on a vendor host, never a slug built from a name."""
    boards: list[tuple[str, str]] = []
    for link in links:
        try:
            host = (urlparse(link).hostname or "").lower()
        except ValueError:
            continue
        if not host or not is_direct_supported_ats_host(host):
            continue
        hit = extract_seed_from_url(link, "")
        if hit is not None and (hit.ats, hit.board_identifier) not in boards:
            boards.append((hit.ats, hit.board_identifier))
    return boards


def verify_proposal(
    p: Proposal, *, client: HttpClient, timeout: int, verify_only: bool = False, page_hops: PageHops | None = None,
) -> VerifyResult:
    """Probe a proposed company/URL to verify a supported ATS board seed."""
    def unresolved(reason: str) -> VerifyResult:
        return VerifyResult(p, "unresolved", None, 0, reason)

    def budget_exhausted() -> VerifyResult:
        """Stop on the global budget, keeping a guessed board that already answered (stored tagged, unconfirmed)."""
        if doubtful is None:
            return unresolved("budget_exhausted")
        _verdict, candidate, seed = doubtful
        return VerifyResult(p, "unresolved", seed, candidate["postings"], "budget_exhausted", candidate=candidate)

    pairs: list[tuple[str, str]] = []
    has_explicit = bool((p.ats.strip() and p.board.strip()) or p.careers_url.strip())
    if p.ats and p.board:
        pairs.append((p.ats.strip().lower(), p.board.strip()))
    if p.careers_url:
        hit = extract_seed_from_url(p.careers_url, p.company)
        if hit is not None:
            pairs.append((hit.ats, hit.board_identifier))
    if not has_explicit:
        # Literal and domain slugs are probed on every vendor before any normalized guess, so "acme" on an
        # early vendor cannot preempt the company's literal "acmegmbh" board on a later one.
        primary, normalized = slug_candidate_passes(p.company, p.domain)
        passes = [primary, normalized]
        own_slug = domain_slug(p.domain)
        if own_slug and _is_short_name(p.company):
            # Only the domain slug can prove a short name's board: probe it on every vendor first.
            passes = [[own_slug], [s for s in primary if s != own_slug], [s for s in normalized if s != own_slug]]
        for slugs in passes:
            pairs.extend((vendor, slug) for vendor in PROBE_VENDORS for slug in slugs)
    # Past its domain slug, no guess can prove a short name's board: keep the first one that answers and stop.
    stop_at_first_hit = not has_explicit and _is_short_name(p.company)

    unsupported = invalid = unparseable = host_budget = vendor_budget = budget_spent = rate_limited = False
    # (reason, candidate, seed) of the best guessed board not proven owned; its seed is still stored, tagged.
    doubtful: tuple[str, dict, AtsBoardSeed | None] | None = None
    skipped_vendors: set[str] = set()
    seen: set[tuple[str, str]] = set()
    answered: dict[tuple[str, str], list] = {}  # boards that answered, with their postings

    def probe(candidates: list[tuple[str, str]], mode: str) -> VerifyResult | None:
        """Try ``candidates``; ``mode``: explicit | guess | company_page | aggregator_page."""
        nonlocal unsupported, invalid, unparseable, host_budget, vendor_budget, budget_spent, rate_limited, doubtful
        for ats, board in candidates:
            key = seed_identity(ats, board)
            try:
                if key in seen:
                    if key not in answered:
                        continue
                    jobs = answered[key]  # a guessed board the page now links: judge it again, no new request
                elif ats in skipped_vendors:
                    continue
                else:
                    seen.add(key)
                    if verify_only:
                        jobs = fetch_vendor_jobs(ats, board, client=client, timeout=timeout, verify_only=True)
                    else:
                        jobs = fetch_vendor_jobs(ats, board, client=client, timeout=timeout)
                    answered[key] = jobs
            except RequestBudgetExceeded as exc:
                scope = getattr(exc, "scope", "global")
                if scope in {"host", "vendor"}:
                    host_budget = host_budget or scope == "host"
                    vendor_budget = vendor_budget or scope == "vendor"
                    skipped_vendors.add(ats)
                    continue
                budget_spent = True
                return None
            except AtsClientError as exc:
                unsupported = unsupported or str(exc) == "vendor_not_supported_yet"
                invalid = invalid or str(exc) == "invalid_board"
                unparseable = unparseable or str(exc) == "unparseable_board"
                continue
            except HttpError as exc:
                # 403/429 (or a host the client stopped calling): this board was never really checked. Other
                # boards still get their request; the client's host-scoped breaker decides what is blocked.
                if isinstance(exc, HostBlocked) or exc.status_code in (403, 429):
                    rate_limited = True
                continue
            except (URLRejected, ValueError, KeyError, TypeError, AttributeError):
                continue
            if mode in {"guess", "aggregator_page"} and not jobs:
                # MARK II verification_probe rule: when probing guessed slugs, an empty
                # jobs array alone is never proof that the company owns that board.
                continue
            if mode == "guess":
                verdict = guessed_board_ownership(p, board, jobs)
                if verdict != "owned":
                    # Keep probing: a later vendor or slug may hold the company's own board.
                    if doubtful is None or (verdict == "ownership_unknown" and doubtful[0] == "wrong_tenant"):
                        doubtful = (
                            verdict, {"ats": ats, "board": board, "postings": len(jobs)},
                            unconfirmed_seed(p, ats, board, jobs, verdict),
                        )
                    if stop_at_first_hit:
                        return None
                    continue
            if mode == "aggregator_page" and not _aggregator_board_is_company(p, board, jobs):
                continue
            try:
                seed = _build_seed(p, ats, board, len(jobs))
            except ValueError:  # vendor outside the supported Literal
                continue
            return VerifyResult(p, "verified", seed, len(jobs), "", tuple(jobs))
        return None

    found = probe(pairs, "explicit" if has_explicit else "guess")
    if found is not None:
        return found
    if budget_spent:
        return budget_exhausted()
    target = _page_hop_target(p, explicit=has_explicit)
    page_skipped = False
    page_unchecked = ""  # why the careers page (or the ATS board it redirects to) was not checked
    if target is not None:
        url, reg, kind = target
        hops = page_hops if page_hops is not None else PageHops()
        page_skipped = not hops.take((urlparse(url).hostname or "").lower())
        if not page_skipped:
            try:
                links = fetch_page_links(url, timeout, client=_DomainScopedPages(client, reg))
            except RequestBudgetExceeded as exc:
                scope = getattr(exc, "scope", "global")
                if scope not in {"host", "vendor"}:
                    return budget_exhausted()
                page_unchecked = f"{scope}_budget_exhausted"
                links = []
            except HostBlocked:  # the page (or the ATS board it redirects to) is blocked: not checked
                page_unchecked = "host_blocked"
                links = []
            boards = _linked_boards(links)[:PAGE_HOP_MAX_BOARDS]
            found = probe(boards, f"{kind}_page")
            if found is not None:
                return found
            if budget_spent:
                return budget_exhausted()
    if doubtful is not None:
        reason, candidate, seed = doubtful
        return VerifyResult(p, "unresolved", seed, candidate["postings"], reason, candidate=candidate)
    if invalid:
        return unresolved("invalid_board")
    if unsupported:
        return unresolved("vendor_not_supported_yet")
    if unparseable:
        return unresolved("unparseable_board")
    if has_explicit and rate_limited:
        return unresolved("host_blocked")
    if has_explicit and vendor_budget:
        return unresolved("vendor_budget_exhausted")
    if has_explicit and host_budget:
        return unresolved("host_budget_exhausted")
    if page_unchecked:
        return unresolved(page_unchecked)
    if page_skipped:  # the careers page was never opened: the call's page budget ran out, not the company
        return unresolved("page_budget_exhausted")
    return unresolved("no_board_found")
