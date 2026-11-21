"""Shared ATS client types, URL helpers, and HTML card-listing parsers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import NoReturn, Protocol
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ultron.infra.net import HostBlocked, RequestBudgetExceeded

# Errors raised before any request (a spent budget, a blocked host or vendor): clients re-raise them untouched, never
# as a board failure and never followed by a fallback request, so callers can report the board as unchecked.
UNCHECKED_ERRORS = (RequestBudgetExceeded, HostBlocked)


@dataclass(slots=True)
class AtsPosting:
    """Normalized job posting returned by an ATS board client."""
    title: str
    apply_url: str
    location: str = ""
    remote_hint: str = ""
    posted_at: str | None = None
    raw_payload: dict | None = None


class AtsClientError(RuntimeError):
    """Raised when an ATS board response or identifier fails vendor validation."""
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class AtsHTTPClient(Protocol):
    """Transport used by the ATS clients (satisfied by ``ultron.infra.net.HttpClient``).

    Failure contract: transport errors surface as ``ultron.infra.net.HttpError`` (with
    ``status_code``) and are NOT wrapped in ``AtsClientError``; callers catch both.
    """

    def request_json(
        self,
        url: str,
        method: str = "GET",
        json_body: dict | None = None,
        timeout: float | None = None,
    ) -> dict | list:
        """Send a request to ``url`` and return the decoded JSON body."""

    def request_text(self, url: str, timeout: float | None = None) -> str:
        """GET ``url`` and return the response body as text."""


def refuse_browser_fallback() -> NoReturn:
    """ULTRON has no headless browser: where MARK III rendered a page in one, fail the board."""
    raise AtsClientError("browser_fallback_unavailable")


def locator_url(locator: str, *parts: str) -> str:
    """Build an ``https://`` URL from a board locator and optional path segments."""
    base = str(locator or "").strip()
    if not base:
        return ""
    if not base.startswith(("http://", "https://")):
        base = f"https://{base.lstrip('/')}"
    for part in parts:
        clean = str(part or "").strip("/")
        if clean:
            base = f"{base.rstrip('/')}/{clean}"
    return base


def locator_has_host(locator: str) -> bool:
    """Return True if ``locator`` contains a dotted hostname."""
    clean = str(locator or "").strip()
    if not clean:
        return False
    if clean.startswith(("http://", "https://")):
        return "." in urlparse(clean).netloc
    return "." in clean.split("/", 1)[0]


def location_text(value: object) -> str:
    """Extract a comma-separated location string from a string or mapping."""
    if isinstance(value, dict):
        name = str(value.get("name") or "").strip()
        if name:
            return name
        parts = [str(value.get(key) or "").strip() for key in ("city", "state", "region", "country")]
        return ", ".join(part for part in parts if part)
    return str(value or "").strip()


def parse_card_listings(
    html: str,
    title_class: str,
    *,
    location_class: str | None = None,
) -> list[dict[str, str]]:
    """Extract job-card listings from board HTML.

    Finds elements whose class attribute contains ``title_class``, resolves the
    nearest anchor for the href, and optionally looks for a sibling/ancestor
    element whose class contains ``location_class``.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    listings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for title_el in soup.select(f'[class*="{title_class}"]'):
        title = title_el.get_text(" ", strip=True)
        if not title:
            continue
        anchor = None
        if title_el.name == "a" and title_el.get("href"):
            anchor = title_el
        if anchor is None:
            anchor = title_el.find("a", href=True)
        if anchor is None:
            anchor = title_el.find_parent("a", href=True)
        href = str(anchor.get("href") or "").strip() if anchor is not None else ""
        if not href:
            continue
        location = ""
        if location_class:
            scope = title_el.parent
            for _ in range(6):
                if scope is None or not hasattr(scope, "select_one"):
                    break
                loc_el = scope.select_one(f'[class*="{location_class}"]')
                if loc_el is not None:
                    location = loc_el.get_text(" ", strip=True)
                    break
                scope = scope.parent
        key = (title, href)
        if key in seen:
            continue
        seen.add(key)
        listings.append({"title": title, "href": href, "location": location})
    return listings


def extract_between(chunk: str, start_marker: str, end_marker: str) -> str | None:
    """Return the substring between the first ``start_marker`` and ``end_marker``."""
    start = chunk.find(start_marker)
    if start == -1:
        return None
    start += len(start_marker)
    end = chunk.find(end_marker, start)
    if end == -1:
        return None
    return chunk[start:end]
