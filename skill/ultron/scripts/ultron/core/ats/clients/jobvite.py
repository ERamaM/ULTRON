from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import AtsClientError, AtsHTTPClient, AtsPosting, locator_url
from ultron.net import RequestBudgetExceeded

logger = logging.getLogger(__name__)

# Markup of a Jobvite career site page, present even when the board has no open jobs.
_CAREER_PAGE_MARKERS = ("jv-page", "jv-job-list")


def _extract_text_between(text: str, marker: str) -> str:
    start = text.find(marker)
    if start == -1:
        return ""
    start += len(marker)
    end = text.find('"', start)
    if end == -1:
        return ""
    return text[start:end].strip()


def _parse_jobvite_html(html: str) -> list[AtsPosting]:
    jobs: list[AtsPosting] = []
    chunks = html.split("jv-job-list")
    for chunk in chunks[1:]:
        title = _extract_text_between(chunk, 'data-title="') or _extract_text_between(chunk, 'title="')
        href = _extract_text_between(chunk, 'href="')
        location = _extract_text_between(chunk, 'data-location="')
        if title and href:
            jobs.append(AtsPosting(title=title, apply_url=href, location=location))
    return jobs


def _parse_jobvite_table(html: str, host: str) -> list[AtsPosting]:
    """Current career sites: ``td.jv-job-list-name`` holds the title anchor, ``td.jv-job-list-location`` the place."""
    soup = BeautifulSoup(html, "html.parser")
    jobs: list[AtsPosting] = []
    for cell in soup.select("td.jv-job-list-name"):
        anchor = cell.find("a", href=True)
        if anchor is None:
            continue
        title = anchor.get_text(" ", strip=True)
        href = str(anchor.get("href") or "").strip()
        if not title or not href:
            continue
        row = cell.find_parent("tr")
        location_cell = row.select_one("td.jv-job-list-location") if row is not None else None
        location = " ".join(location_cell.get_text(" ", strip=True).split()) if location_cell is not None else ""
        location = location.replace(" ,", ",")
        apply_url = href if href.startswith(("http://", "https://")) else locator_url(host, href)
        jobs.append(AtsPosting(title=title, apply_url=apply_url, location=location))
    return jobs


def fetch_jobvite_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    # EU-resident boards live on jobs.eu.jobvite.com; retry there when the
    # primary host errors or yields no postings.
    fetch_text = client.request_text
    fetched = career_page = False
    last_exc: Exception | None = None
    for host in ("jobs.jobvite.com", "jobs.eu.jobvite.com"):
        try:
            html = fetch_text(locator_url(host, board_identifier), timeout=timeout)
        except RequestBudgetExceeded:
            raise
        except Exception as exc:
            last_exc = exc
            continue
        fetched = True
        career_page = career_page or any(marker in (html or "") for marker in _CAREER_PAGE_MARKERS)
        jobs = _parse_jobvite_table(html, host) or _parse_jobvite_html(html)
        if jobs:
            return jobs
    if not fetched and last_exc is not None:
        raise last_exc
    if not career_page:
        # An unknown tenant redirects to the vendor's support site: never report it as a board with no jobs.
        raise AtsClientError("board_not_found")
    logger.warning(
        "jobvite parsed zero jobs board=%s; page fetched but no postings matched selectors",
        board_identifier,
    )
    return []
