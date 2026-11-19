from __future__ import annotations

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
            html = fetch_text(f"{board_url}?pr={page}", timeout=timeout)
        except RequestBudgetExceeded:
            raise
        except Exception:
            if page > 0:
                break
            refuse_browser_fallback()
        added = 0
        for listing in parse_card_listings(html, "iCIMS_JobsTable_title", location_class="iCIMS_JobsTable_location"):
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
    # Empty board (or one whose HTML we couldn't parse) is not an error.
    return postings
