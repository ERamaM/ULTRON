from __future__ import annotations

import logging

from ultron.core.ats.clients.base import (
    UNCHECKED_ERRORS,
    AtsClientError,
    AtsHTTPClient,
    AtsPosting,
    location_text,
    locator_url,
    parse_card_listings,
)

logger = logging.getLogger(__name__)

# BambooHR ``locationType``: "0" on-site (seen on a live onsite posting), "1" remote, "2" hybrid.
_LOCATION_TYPES = {"0": "On-site", "1": "Remote", "2": "Hybrid"}


def bamboohr_workplace(job: object) -> str:
    """Workplace label of a BambooHR list row or detail ``jobOpening`` (``""`` when unknown)."""
    if not isinstance(job, dict):
        return ""
    location_type = job.get("locationType")
    label = _LOCATION_TYPES.get(str(location_type).strip()) if isinstance(location_type, (str, int)) else None
    if label:
        return label
    return "Remote" if job.get("isRemote") is True else ""


def fetch_bamboohr_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_json = client.request_json
    fetch_text = client.request_text
    host = f"{board_identifier}.bamboohr.com"
    # The /careers page is a JS shell; the real data is the JSON endpoint.
    try:
        payload = fetch_json(locator_url(host, "careers", "list"), timeout=timeout)
    except UNCHECKED_ERRORS:
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
                        remote_hint=bamboohr_workplace(job),
                        raw_payload=job,
                    )
                )
    if postings or (isinstance(payload, dict) and isinstance(payload.get("result"), list) and not rows):
        # A JSON list, even an empty one, is the tenant's answer: an empty board needs no HTML fallback.
        return postings

    # Fallback: scrape the rendered careers page HTML.
    html = fetch_text(locator_url(host, "careers"), timeout=timeout)
    if payload is None and "BambooHR-ATS" not in (html or ""):
        # No JSON and no careers markup: an unknown tenant redirects to the vendor's marketing site.
        raise AtsClientError("board_not_found")
    for listing in parse_card_listings(
        html, "BambooHR-ATS-Jobs-Item-Title", location_class="BambooHR-ATS-Jobs-Item-Location"
    ):
        apply_url = listing["href"]
        if not apply_url.startswith(("http://", "https://")):
            apply_url = locator_url(host, apply_url)
        postings.append(AtsPosting(title=listing["title"], apply_url=apply_url, location=listing["location"]))
    return postings
