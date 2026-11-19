from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from ultron.core.ats.clients.base import AtsHTTPClient, AtsPosting
from ultron.core.ats.vendors import greenhouse_job_url
from ultron.net import RequestBudgetExceeded, URLRejected


def _canonical_greenhouse_url(board_identifier: str, job: dict, *, eu: bool = False) -> str:
    job_id = str(job.get("id") or "").strip()
    if job_id.isdigit():
        if eu:
            return f"https://boards.eu.greenhouse.io/{board_identifier}/jobs/{job_id}"
        return greenhouse_job_url(board_identifier, job_id)

    absolute_url = str(job.get("absolute_url") or "").strip()
    if absolute_url:
        parsed = urlparse(absolute_url)
        gh_jid = parse_qs(parsed.query).get("gh_jid", [""])[0].strip()
        if gh_jid.isdigit():
            if eu:
                return f"https://boards.eu.greenhouse.io/{board_identifier}/jobs/{gh_jid}"
            return greenhouse_job_url(board_identifier, gh_jid)

    return absolute_url


def fetch_greenhouse_jobs(board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient) -> list[AtsPosting]:
    fetch_json = client.request_json
    payload = None
    used_eu = False
    last_exc: Exception | None = None
    for host, is_eu in (("boards-api.greenhouse.io", False), ("boards-api.eu.greenhouse.io", True)):
        url = f"https://{host}/v1/boards/{board_identifier}/jobs?content=false"
        try:
            payload = fetch_json(url, timeout=timeout)
            used_eu = is_eu
            break
        except (RequestBudgetExceeded, URLRejected):
            raise
        except Exception as exc:
            last_exc = exc
            continue
    if payload is None:
        if last_exc is not None:
            raise last_exc
        return []
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []

    output: list[AtsPosting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title", "")).strip()
        apply_url = _canonical_greenhouse_url(board_identifier, job, eu=used_eu)
        if not title or not apply_url:
            continue
        location = ""
        location_payload = job.get("location")
        if isinstance(location_payload, dict):
            location = str(location_payload.get("name", "")).strip()
        posted_at = str(job.get("updated_at") or job.get("created_at") or "").strip() or None
        output.append(
            AtsPosting(
                title=title,
                apply_url=apply_url,
                location=location,
                posted_at=posted_at,
                raw_payload=job,
            )
        )
    return output
