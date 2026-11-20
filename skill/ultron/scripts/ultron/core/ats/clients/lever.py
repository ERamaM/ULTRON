from __future__ import annotations

from ultron.core.ats.clients.base import UNCHECKED_ERRORS, AtsHTTPClient, AtsPosting


def fetch_lever_jobs(board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient) -> list[AtsPosting]:
    # EU-resident boards live on api.eu.lever.co; retry there when the
    # primary host errors or returns an empty list.
    fetch_json = client.request_json
    jobs: list | None = None
    last_exc: Exception | None = None
    for host in ("api.lever.co", "api.eu.lever.co"):
        url = f"https://{host}/v0/postings/{board_identifier}?mode=json"
        try:
            payload = fetch_json(url, timeout=timeout)
        except UNCHECKED_ERRORS:
            raise
        except Exception as exc:
            last_exc = exc
            continue
        jobs = payload if isinstance(payload, list) else []
        if jobs:
            break
    if jobs is None:
        # Transport failure on every host is an error; a board that simply had
        # no postings (jobs == []) falls through and returns an empty list.
        if last_exc is not None:
            raise last_exc
        return []

    output: list[AtsPosting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("text") or "").strip()
        apply_url = str(job.get("hostedUrl") or "").strip()
        if not title or not apply_url:
            continue
        categories = job.get("categories") if isinstance(job.get("categories"), dict) else {}
        locations = [str(categories.get("location") or "").strip()]
        all_locations = categories.get("allLocations")
        if isinstance(all_locations, list):
            locations.extend(item.strip() for item in all_locations if isinstance(item, str))
        posted_at = str(job.get("createdAt") or "").strip() or None
        output.append(
            AtsPosting(
                title=title,
                apply_url=apply_url,
                location="; ".join(dict.fromkeys(filter(None, locations))),
                remote_hint=str(job.get("workplaceType") or "").strip(),
                posted_at=posted_at,
                raw_payload=job,
            )
        )
    return output
