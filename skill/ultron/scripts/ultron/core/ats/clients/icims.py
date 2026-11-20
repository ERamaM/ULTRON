from __future__ import annotations

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import (
    AtsClientError,
    AtsHTTPClient,
    AtsPosting,
    locator_has_host,
    locator_url,
    parse_card_listings,
    refuse_browser_fallback,
)
from ultron.net import RequestBudgetExceeded

_ICIMS_MAX_PAGES = 25  # Safety cap: ~20 rows per page
# Markup only a listing page has (legacy table rows or current job cards, even with zero rows). A portal's
# /jobs/search page is an iframe shell without it; the listings come with ``in_iframe=1``.
_LISTING_MARKERS = ("iCIMS_JobsTable", "iCIMS_JobCardItem", "iCIMS_JobListingRow")


def _card_listings(html: str) -> list[dict[str, str]]:
    """Current job cards: ``a.iCIMS_Anchor`` wraps the title heading, the location sits in the card header."""
    soup = BeautifulSoup(html, "html.parser")
    listings: list[dict[str, str]] = []
    for card in soup.select('[class*="iCIMS_JobCardItem"]'):
        anchor = card.select_one("a.iCIMS_Anchor[href]")
        if anchor is None:
            continue
        heading = anchor.find(["h1", "h2", "h3", "h4"])
        title = (heading or anchor).get_text(" ", strip=True)
        location = ""
        header = card.select_one(".header.left")
        if header is not None:
            for label in header.select(".sr-only"):
                label.decompose()
            location = header.get_text(" ", strip=True)
        href = str(anchor.get("href") or "").strip()
        if title and href:
            listings.append({"title": title, "href": href, "location": location})
    return listings


def fetch_icims_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_text = client.request_text
    if not locator_has_host(board_identifier):
        raise AtsClientError(f"icims_locator_invalid={board_identifier}")
    board_url = locator_url(board_identifier, "jobs", "search")
    postings: list[AtsPosting] = []
    seen: set[str] = set()
    for page in range(_ICIMS_MAX_PAGES):
        try:
            html = fetch_text(f"{board_url}?pr={page}&in_iframe=1", timeout=timeout)
        except RequestBudgetExceeded:
            raise
        except Exception:
            if page > 0:
                break
            refuse_browser_fallback()
        if page == 0 and not any(marker in (html or "") for marker in _LISTING_MARKERS):
            # No listing markup at all (a wrapper or script shell): never report it as a board with no jobs.
            raise AtsClientError("unparseable_board")
        added = 0
        listings = parse_card_listings(html, "iCIMS_JobsTable_title", location_class="iCIMS_JobsTable_location")
        for listing in listings or _card_listings(html):
            apply_url = listing["href"]
            if not apply_url.startswith(("http://", "https://")):
                apply_url = locator_url(board_identifier, apply_url)
            if apply_url in seen:
                continue
            seen.add(apply_url)
            postings.append(
                AtsPosting(
                    title=listing["title"],
                    apply_url=apply_url,
                    location=listing["location"],
                )
            )
            added += 1
        if added == 0:
            break
    # A listing page without rows is an empty board, not an error.
    return postings
