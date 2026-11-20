from __future__ import annotations

from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import (
    AtsClientError,
    AtsHTTPClient,
    AtsPosting,
    refuse_browser_fallback,
)
from ultron.core.ats.vendors import domain_matches
from ultron.net import RequestBudgetExceeded

_COMEET_HOST = "https://www.comeet.co"
_COMEET_DOMAINS = ("comeet.com", "comeet.co")


def _parse_positions(html: str, slug: str) -> list[AtsPosting]:
    # Hosted career pages link positions as /jobs/{slug}/{companyUid}/{positionUid}/{name}.
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    prefix = f"/jobs/{slug.lower()}/"
    out: list[AtsPosting] = []
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"]).strip()
        path = href.lower()
        if path.startswith("http"):
            # Hosted positions live on www.comeet.com (www.comeet.co redirects there). Match the parsed host:
            # "comeet.co" is a substring of "comeet.com", so splitting the URL text on it breaks the path.
            parsed = urlparse(path)
            if not any(domain_matches(parsed.hostname or "", domain) for domain in _COMEET_DOMAINS):
                continue
            path = parsed.path
        if not path.startswith(prefix):
            continue
        # Require at least company-uid + position segments beyond the slug.
        if len([part for part in path[len(prefix) :].split("/") if part]) < 2:
            continue
        title = " ".join(anchor.get_text(" ", strip=True).split())
        if not title:
            continue
        apply_url = urljoin(_COMEET_HOST, href)
        if apply_url in seen:
            continue
        seen.add(apply_url)
        out.append(AtsPosting(title=title, apply_url=apply_url, location=""))
    return out


def fetch_comeet_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_text = client.request_text
    slug = str(board_identifier or "").strip().strip("/")
    if not slug:
        raise AtsClientError(f"comeet_locator_invalid={board_identifier}")
    board_url = f"{_COMEET_HOST}/jobs/{slug}"
    try:
        html = fetch_text(board_url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception:
        html = ""
    postings = _parse_positions(html, slug)
    if not postings:
        # Hosted pages are Angular-rendered and ULTRON has no browser: static HTML without
        # listings (or a failed fetch) is reported as an error, never as an empty board.
        refuse_browser_fallback()
    return postings
