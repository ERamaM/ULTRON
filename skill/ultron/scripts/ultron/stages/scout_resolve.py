"""Scout finds: turn each URL the agent found into a posting ULTRON can fetch, or say why it cannot.

A find on a supported ATS board is a posting as it is. A find on an employer's own careers page is kept as an
``employer_page`` posting, never discarded for being outside the ATS allowlist. A find on a job aggregator is
resolved to the employer posting it links to; when that fails it is stored as ``unresolved_aggregator`` and
never exported. Page text is data: only links are read from it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ultron.core.ats.registry import extract_seed_from_url, infer_vendor_from_host
from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.feeds.urls import PageLinksUnavailable, fetch_page_links, has_employer_job_path, is_aggregator_host
from ultron.core.job_identity import canonical_job_identity
from ultron.infra.net import HostBlocked, RequestBudgetExceeded, VendorBlocked
from ultron.infra.urlpolicy import URLRejected, admit_url
from ultron.seeds.naming import registrable_domain

EMPLOYER_PAGE = "employer_page"
# Words that mark a staffing agency posting for an unnamed client; such a listing never becomes a seed.
_RECRUITER_RE = re.compile(
    r"\b(?:recruit(?:ment|ing|ers?)?|staffing|headhunt(?:ers?|ing)?|talent\s+(?:connect|partners?|solutions?)"
    r"|jobgether|hiring\s+partner)\b",
    re.IGNORECASE,
)
MAX_AGGREGATOR_LINKS = 400


@dataclass
class Resolution:
    """The outcome of one find: what it is, and the URL ULTRON fetches for it (when there is one)."""

    outcome: str
    url: str = ""
    source_type: str = ""
    reason: str = ""


def is_recruiter(company: str) -> bool:
    """Whether ``company`` names a staffing agency rather than the employer."""
    return bool(_RECRUITER_RE.search(str(company or "")))


def is_job_link(url: str) -> bool:
    """A link to one posting on a supported ATS board (not a board root, not a marketing page)."""
    host = domain_of(normalize_url(url))
    if not infer_vendor_from_host(host) or extract_seed_from_url(url, "") is None:
        return False
    return not canonical_job_identity(url).identity_key.startswith(("url:", "raw:"))


def classify_find(url: str) -> Resolution:
    """What a find is, from its URL alone (no network)."""
    try:
        admit_url(url)
    except URLRejected as exc:
        return Resolution("rejected", reason=str(exc) or "url_rejected")
    host = domain_of(normalize_url(url))
    if infer_vendor_from_host(host):
        return Resolution("ats_posting", url=url, source_type="web_search")
    if is_aggregator_host(host):
        return Resolution("aggregator", url=url)
    return Resolution(EMPLOYER_PAGE, url=url, source_type=EMPLOYER_PAGE)


def _company_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").casefold())


def _pick_link(links: list[str], company: str) -> str | None:
    """The one posting link of the page; with several, the one on the board of ``company``; else ``None``."""
    by_identity: dict[str, str] = {}
    for link in links:
        by_identity.setdefault(canonical_job_identity(link).identity_key, link)
    wanted = _company_key(company)
    if len(by_identity) == 1 and not wanted:
        return next(iter(by_identity.values()))
    if not wanted:
        return None
    matching = [link for link in by_identity.values()
                if (seed := extract_seed_from_url(link, "")) is not None
                and (wanted in _company_key(seed.board_identifier) or _company_key(seed.board_identifier) in wanted)]
    return matching[0] if len(matching) == 1 else None


_NOT_EMPLOYERS = frozenset({"linkedin.com", "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
                            "google.com", "apple.com", "github.com", "medium.com", "t.me", "wa.me"})


def employer_links(links: list[str], page_url: str, company: str) -> list[str]:
    """Links to one opening on an employer's own site (not the aggregator, another aggregator or a social site).

    With a ``company``, only links on a host that names it count, so related jobs of other employers never win.
    """
    page_domain = registrable_domain(domain_of(normalize_url(page_url)))
    wanted = _company_key(company)
    out = []
    for link in links:
        host = domain_of(normalize_url(link))
        domain = registrable_domain(host) or host
        if domain == page_domain or domain in _NOT_EMPLOYERS or is_aggregator_host(host):
            continue
        if not has_employer_job_path(link):
            continue
        if wanted and wanted not in _company_key(host):
            continue
        out.append(link)
    return list(dict.fromkeys(out))


def resolve_aggregator(url: str, client, timeout: int, company: str = "") -> Resolution:
    """The employer posting an aggregator page links to, or ``unresolved_aggregator``.

    Links to a single posting on a supported ATS board count first: an aggregator's own pages and links to other
    aggregators are ignored. When the page links to several postings (related jobs), only the one on the board
    of ``company`` is taken; if that does not single one out, the find stays unresolved rather than risk another
    employer's job. With no ATS link, one link to an opening on the employer's own site is followed (an employer
    page). A blocked host or an exhausted budget leaves the find unresolved with that reason.
    """
    try:
        links = fetch_page_links(url, timeout, client=client, require_completed=True)[:MAX_AGGREGATOR_LINKS]
    except (RequestBudgetExceeded, HostBlocked, VendorBlocked, PageLinksUnavailable) as exc:
        return Resolution("unresolved_aggregator", reason=type(exc).__name__)
    postings = []
    for link in links:
        try:
            admit_url(link)
        except URLRejected:
            continue
        if is_job_link(link):
            postings.append(link)
    if not postings:
        direct = employer_links(links, url, company)
        if len(direct) == 1:
            return Resolution("resolved_aggregator", url=direct[0], source_type=EMPLOYER_PAGE)
        return Resolution("unresolved_aggregator", reason="no_employer_posting_link")
    chosen = _pick_link(postings, company)
    if chosen is None:
        return Resolution("unresolved_aggregator", reason="ambiguous_posting_links")
    return Resolution("resolved_aggregator", url=chosen, source_type="web_search")
