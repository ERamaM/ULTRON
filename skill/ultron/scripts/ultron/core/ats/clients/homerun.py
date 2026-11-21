"""Homerun public job-board client."""

from __future__ import annotations

from ultron.core.ats.clients.base import AtsHTTPClient, AtsPosting, locator_url


def fetch_homerun_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    """Fetch open postings from a Homerun board."""
    fetch_json = client.request_json
    url = locator_url(f"{board_identifier}.homerun.co", "api", "jobs")
    payload = fetch_json(url, timeout=timeout)
    jobs = payload.get("jobs", payload) if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        jobs = []
    out: list[AtsPosting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title") or job.get("name") or "").strip()
        apply_url = str(job.get("url") or job.get("careers_url") or "").strip()
        if not apply_url:
            slug = str(job.get("slug") or job.get("id") or "").strip()
            if slug:
                apply_url = locator_url(f"{board_identifier}.homerun.co", slug)
        location = str(job.get("location") or job.get("location_name") or "").strip()
        posted_at = str(job.get("published_at") or job.get("created_at") or "").strip() or None
        if title and apply_url:
            out.append(
                AtsPosting(title=title, apply_url=apply_url, location=location, posted_at=posted_at, raw_payload=job)
            )
    return out
