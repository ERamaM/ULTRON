from __future__ import annotations

from ultron.core.ats.clients.base import AtsHTTPClient, AtsPosting, location_text


def workable_locations(job: dict) -> list[str]:
    """Read public locations from both board and detail responses."""
    locations = job.get("locations")
    if isinstance(locations, list) and locations:
        return list(dict.fromkeys(
            value for item in locations
            if isinstance(item, dict) and item.get("hidden") is not True
            if (value := location_text(item))
        ))
    fallback = location_text(job.get("location")) or location_text({
        key: job.get(key) for key in ("city", "state", "country")
    })
    return [fallback] if fallback else []


def fetch_workable_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    # Public widget API used by Workable hosted boards.
    fetch_json = client.request_json
    url = f"https://apply.workable.com/api/v1/widget/accounts/{board_identifier}?details=true"
    payload = fetch_json(url, timeout=timeout)
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []

    postings: dict[str, AtsPosting] = {}
    locations_by_url: dict[str, dict[str, None]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title", "")).strip()
        apply_url = str(job.get("url", "")).strip()
        if not title or not apply_url:
            continue
        # The widget repeats the same URL once per location.
        locations = locations_by_url.setdefault(apply_url, {})
        locations.update(dict.fromkeys(workable_locations(job)))
        if apply_url not in postings:
            postings[apply_url] = AtsPosting(
                title=title,
                apply_url=apply_url,
                remote_hint="Remote" if job.get("telecommuting") is True else "",
                posted_at=str(job.get("published_on") or job.get("published") or job.get("created_at") or "").strip() or None,
                raw_payload=job,
            )
    for url, posting in postings.items():
        posting.location = "; ".join(locations_by_url[url])
    return list(postings.values())
