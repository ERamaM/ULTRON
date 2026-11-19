from __future__ import annotations

import logging

from ultron.core.ats.clients.base import (
    AtsHTTPClient,
    AtsPosting,
    locator_url,
    parse_card_listings,
)
from ultron.net import HttpError, RequestBudgetExceeded

logger = logging.getLogger(__name__)


def fetch_pinpoint_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_json = client.request_json
    fetch_text = client.request_text
    api_url = locator_url(f"{board_identifier}.pinpointhq.com", "api", "jobs")
    try:
        payload = fetch_json(api_url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except HttpError as exc:
        if exc.status_code == 404:
            raise
        logger.warning(
            "pinpoint api fetch failed board=%s err=%s; falling back to HTML scraping",
            board_identifier,
            exc,
        )
        payload = None
    except Exception as exc:
        logger.warning(
            "pinpoint api fetch failed board=%s err=%s; falling back to HTML scraping",
            board_identifier,
            exc,
        )
        payload = None
    jobs = payload.get("jobs", payload) if isinstance(payload, dict) else payload
    if isinstance(jobs, list):
        out: list[AtsPosting] = []
        for job in jobs:
            if not isinstance(job, dict):
                continue
            title = str(job.get("title") or job.get("name") or "").strip()
            apply_url = str(job.get("url") or job.get("apply_url") or "").strip()
            if title and apply_url:
                out.append(
                    AtsPosting(
                        title=title,
                        apply_url=apply_url,
                        location=str(job.get("location") or "").strip(),
                        raw_payload=job,
                    )
                )
        if out:
            return out

    html = fetch_text(locator_url(f"{board_identifier}.pinpointhq.com"), timeout=timeout)
    out = []
    for listing in parse_card_listings(html, "posting-card__title"):
        apply_url = listing["href"]
        if not apply_url.startswith(("http://", "https://")):
            apply_url = locator_url(f"{board_identifier}.pinpointhq.com", apply_url)
        out.append(
            AtsPosting(
                title=listing["title"],
                apply_url=apply_url,
            )
        )
    return out
