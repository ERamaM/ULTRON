from __future__ import annotations

import logging

from ultron.core.ats.clients.base import AtsHTTPClient, AtsPosting, locator_url
from ultron.net import RequestBudgetExceeded

logger = logging.getLogger(__name__)


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


def fetch_jobvite_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    # EU-resident boards live on jobs.eu.jobvite.com; retry there when the
    # primary host errors or yields no postings.
    fetch_text = client.request_text
    fetched = False
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
        jobs = _parse_jobvite_html(html)
        if jobs:
            return jobs
    if not fetched and last_exc is not None:
        raise last_exc
    logger.warning(
        "jobvite parsed zero jobs board=%s; page fetched but no postings matched selectors",
        board_identifier,
    )
    return []
