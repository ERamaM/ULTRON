"""JazzHR public job-board HTML client."""

from __future__ import annotations

from urllib.parse import urljoin

from ultron.core.ats.clients.base import (
    AtsHTTPClient,
    AtsPosting,
    extract_between,
    locator_url,
)


def fetch_jazzhr_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    """Fetch open postings from a JazzHR hosted board."""
    fetch_text = client.request_text
    board_url = locator_url(f"{board_identifier}.applytojob.com")
    html = fetch_text(board_url, timeout=timeout)
    postings: list[AtsPosting] = []
    for chunk in html.split("job-listing"):
        href = extract_between(chunk, 'href="', '"')
        title = extract_between(chunk, 'job-title">', "<")
        if href is None or title is None:
            continue
        href = href.strip()
        title = title.strip()
        if title and href:
            postings.append(AtsPosting(title=title, apply_url=urljoin(board_url, href)))
    return postings
