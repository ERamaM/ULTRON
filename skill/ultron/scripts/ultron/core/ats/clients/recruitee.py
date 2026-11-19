from __future__ import annotations

from ultron.core.ats.clients.base import AtsHTTPClient, AtsPosting, locator_url


def fetch_recruitee_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_json = client.request_json
    url = locator_url(f"{board_identifier}.recruitee.com", "api", "offers")
    payload = fetch_json(url, timeout=timeout)
    offers = payload.get("offers", []) if isinstance(payload, dict) else []
    out: list[AtsPosting] = []
    for job in offers:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title") or "").strip()
        careers_url = str(job.get("careers_url") or job.get("url") or "").strip()
        location = str(job.get("location") or job.get("location_name") or "").strip()
        posted_at = str(job.get("published_at") or job.get("created_at") or "").strip() or None
        if title and careers_url:
            out.append(
                AtsPosting(title=title, apply_url=careers_url, location=location, posted_at=posted_at, raw_payload=job)
            )
    return out
