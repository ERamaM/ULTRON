from __future__ import annotations

import logging

from ultron.core.ats.clients.base import (
    AtsHTTPClient,
    AtsPosting,
    location_text,
    locator_url,
    parse_card_listings,
)
from ultron.net import RequestBudgetExceeded

logger = logging.getLogger(__name__)


def fetch_bamboohr_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_json = client.request_json
    fetch_text = client.request_text
    host = f"{board_identifier}.bamboohr.com"
    # The /careers page is a JS shell; the real data is the JSON endpoint.
    try:
        payload = fetch_json(locator_url(host, "careers", "list"), timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        logger.warning(
            "bamboohr api fetch failed board=%s err=%s; falling back to HTML scraping",
            board_identifier,
            exc,
        )
        payload = None
    rows = payload.get("result", []) if isinstance(payload, dict) else []
    postings: list[AtsPosting] = []
    if isinstance(rows, list):
        for job in rows:
            if not isinstance(job, dict):
                continue
            title = str(job.get("jobOpeningName") or job.get("title") or "").strip()
            job_id = str(job.get("id") or "").strip()
            location = location_text(job.get("location"))
            if title and job_id:
                postings.append(
                    AtsPosting(
                        title=title,
                        apply_url=locator_url(host, "careers", job_id),
                        location=location,
                        raw_payload=job,
                    )
                )
    if postings:
        return postings

    # Fallback: scrape the rendered careers page HTML.
    html = fetch_text(locator_url(host, "careers"), timeout=timeout)
    for listing in parse_card_listings(
        html, "BambooHR-ATS-Jobs-Item-Title", location_class="BambooHR-ATS-Jobs-Item-Location"
    ):
        apply_url = listing["href"]
        if not apply_url.startswith(("http://", "https://")):
            apply_url = locator_url(host, apply_url)
        postings.append(AtsPosting(title=listing["title"], apply_url=apply_url, location=listing["location"]))
    return postings
