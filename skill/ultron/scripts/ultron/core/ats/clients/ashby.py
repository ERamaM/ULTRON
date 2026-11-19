from __future__ import annotations

from ultron.core.ats.clients.base import AtsClientError, AtsHTTPClient, AtsPosting

ASHBY_API_HOST = "api.ashbyhq.com"


def fetch_ashby_jobs(board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient) -> list[AtsPosting]:
    url = f"https://{ASHBY_API_HOST}/posting-api/job-board/{board_identifier}"
    payload = client.request_json(url, timeout=timeout)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise AtsClientError("unexpected_payload")

    output: list[AtsPosting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title") or "").strip()
        job_url = str(job.get("jobUrl") or "").strip()
        if not title or not job_url or not job.get("isListed", True):
            continue
        output.append(AtsPosting(
            title=title,
            apply_url=job_url,
            location=str(job.get("location") or "").strip(),
            remote_hint=str(job.get("workplaceType") or "").strip(),
            posted_at=str(job.get("publishedAt") or "").strip() or None,
            raw_payload={key: value for key, value in job.items() if key not in {"descriptionHtml", "descriptionPlain"}},
        ))
    return output
