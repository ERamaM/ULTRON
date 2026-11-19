from __future__ import annotations

from ultron.core.ats.clients.base import AtsClientError, AtsHTTPClient, AtsPosting, location_text

_PAGE_LIMIT = 100
_MAX_PAGES = 10  # Safety cap: max 1000 jobs per board


def smartrecruiters_location(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("fullLocation") or "").strip() or location_text(value)
    return location_text(value)


def smartrecruiters_workplace(location: object) -> str:
    if isinstance(location, dict):
        if location.get("hybrid") is True:
            return "Hybrid"
        if location.get("remote") is True:
            return "Remote"
    return ""


def fetch_smartrecruiters_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_json = client.request_json
    out: list[AtsPosting] = []
    offset = 0
    for _ in range(_MAX_PAGES):
        url = f"https://api.smartrecruiters.com/v1/companies/{board_identifier}/postings?limit={_PAGE_LIMIT}&offset={offset}"
        payload = fetch_json(url, timeout=timeout)
        jobs = payload.get("content", []) if isinstance(payload, dict) else []
        if not jobs:
            if offset == 0:
                # SmartRecruiters returns HTTP 200 with totalFound=0 and content=[] for
                # every nonexistent company slug; fail closed so slug probing cannot
                # falsely verify arbitrary names as SmartRecruiters boards.
                raise AtsClientError("board_not_found")
            break
        for job in jobs:
            if not isinstance(job, dict):
                continue
            title = str(job.get("name") or job.get("title") or "").strip()
            job_id = str(job.get("id") or "").strip()
            # Prefer canonical careers.smartrecruiters.com/<board>/<id> when job_id exists so
            # downstream fetch + API enrichment keep the posting id even if jobAdUrl points
            # to a generic corporate career landing page (e.g. jobs.bosch.com/en, sika.com).
            raw_job_ad = str(job.get("jobAdUrl") or "").strip()
            if job_id:
                apply_url = f"https://careers.smartrecruiters.com/{board_identifier}/{job_id}"
            else:
                apply_url = str(raw_job_ad or job.get("applyUrl") or job.get("ref") or "").strip()
            location = job.get("location") or job.get("locationName")
            posted_at = str(job.get("releasedDate") or job.get("createdOn") or "").strip() or None
            if title and apply_url:
                out.append(
                    AtsPosting(
                        title=title, apply_url=apply_url, location=smartrecruiters_location(location),
                        remote_hint=smartrecruiters_workplace(location), posted_at=posted_at, raw_payload=job,
                    )
                )
        if len(jobs) < _PAGE_LIMIT:
            break
        offset += _PAGE_LIMIT
    return out
