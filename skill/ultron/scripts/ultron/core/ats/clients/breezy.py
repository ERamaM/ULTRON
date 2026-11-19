from __future__ import annotations

from ultron.core.ats.clients.base import AtsHTTPClient, AtsPosting, location_text, locator_url


def fetch_breezy_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_json = client.request_json
    url = locator_url(f"{board_identifier}.breezy.hr", "json")
    payload = fetch_json(url, timeout=timeout)
    jobs = payload.get("jobs", payload) if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        jobs = []
    out: list[AtsPosting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("name") or job.get("title") or "").strip()
        apply_url = str(job.get("url") or job.get("public_url") or "").strip()
        if not apply_url:
            slug = str(job.get("slug") or job.get("id") or "").strip()
            if slug:
                apply_url = locator_url(f"{board_identifier}.breezy.hr", "p", slug)
        location = location_text(job.get("location") or job.get("location_name"))
        posted_at = str(job.get("created_at") or job.get("published_at") or "").strip() or None
        if title and apply_url:
            out.append(
                AtsPosting(title=title, apply_url=apply_url, location=location, posted_at=posted_at, raw_payload=job)
            )
    return out
