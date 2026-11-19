from __future__ import annotations

import logging

from ultron.core.ats.clients.base import (
    AtsHTTPClient,
    AtsPosting,
    locator_url,
    parse_card_listings,
)
from ultron.net import RequestBudgetExceeded

logger = logging.getLogger(__name__)


def fetch_factorial_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_json = client.request_json
    fetch_text = client.request_text
    api_url = locator_url("jobs.factorialhr.com", board_identifier, "api", "jobs")
    try:
        payload = fetch_json(api_url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        logger.warning(
            "factorial api fetch failed board=%s err=%s; falling back to HTML scraping",
            board_identifier,
            exc,
        )
        payload = None
    if isinstance(payload, dict):
        jobs = payload.get("jobs", [])
    elif isinstance(payload, list):
        jobs = payload
    else:
        jobs = []
    out: list[AtsPosting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title") or job.get("name") or "").strip()
        apply_url = str(job.get("url") or job.get("apply_url") or "").strip()
        location = str(job.get("location") or "").strip()
        if title and apply_url:
            out.append(AtsPosting(title=title, apply_url=apply_url, location=location, raw_payload=job))
    if out:
        return out

    html = fetch_text(locator_url("jobs.factorialhr.com", board_identifier), timeout=timeout)
    for listing in parse_card_listings(html, "job-card__title"):
        apply_url = listing["href"]
        if not apply_url.startswith(("http://", "https://")):
            apply_url = locator_url("jobs.factorialhr.com", apply_url)
        out.append(
            AtsPosting(
                title=listing["title"],
                apply_url=apply_url,
            )
        )
    return out
