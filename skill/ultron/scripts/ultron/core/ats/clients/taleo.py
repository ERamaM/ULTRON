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


def fetch_taleo_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_text = client.request_text
    if not locator_has_host(board_identifier):
        raise AtsClientError(f"taleo_locator_invalid={board_identifier}")
    board_url = locator_url(board_identifier)
    try:
        html = fetch_text(board_url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception:
        refuse_browser_fallback()
    postings: list[AtsPosting] = []
    for listing in parse_card_listings(html, "jobTitle-link"):
        apply_url = listing["href"]
        if not apply_url.startswith(("http://", "https://")):
            apply_url = locator_url(board_identifier, apply_url)
        postings.append(
            AtsPosting(
                title=listing["title"],
                apply_url=apply_url,
            )
        )
    # Empty board (or unparseable HTML) is not an error.
    return postings
