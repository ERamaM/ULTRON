from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import (
    UNCHECKED_ERRORS,
    AtsClientError,
    AtsHTTPClient,
    AtsPosting,
    locator_has_host,
    locator_url,
    parse_card_listings,
    refuse_browser_fallback,
)

# Empty-state wording of a career section with no openings. Without listings or this wording the markup is
# unparseable (script-rendered or changed), which is reported as an error rather than as an empty board.
_EMPTY_BOARD_RE = re.compile(
    r"\bno (?:jobs|job openings|open positions|openings|vacancies)\b"
    r"|\bthere are (?:currently )?no (?:jobs|job openings|open positions|openings|vacancies)\b",
    re.IGNORECASE,
)


def _is_empty_board(html: str) -> bool:
    text = " ".join(BeautifulSoup(html or "", "html.parser").get_text(" ").split())
    return bool(_EMPTY_BOARD_RE.search(text))


def fetch_taleo_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_text = client.request_text
    if not locator_has_host(board_identifier):
        raise AtsClientError(f"taleo_locator_invalid={board_identifier}")
    board_url = locator_url(board_identifier)
    try:
        html = fetch_text(board_url, timeout=timeout)
    except UNCHECKED_ERRORS:
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
    # No pagination: only the first page of a career section is read (multi-page Taleo boards are a no-go).
    if not postings and not _is_empty_board(html):
        raise AtsClientError("unparseable_board")
    return postings
