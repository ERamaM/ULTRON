from __future__ import annotations

from ultron.core.ats.clients.base import AtsHTTPClient, AtsPosting


def fetch_workable_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    # Public widget API used by Workable hosted boards.
    fetch_json = client.request_json
    url = f"https://apply.workable.com/api/v1/widget/accounts/{board_identifier}?details=true"
    payload = fetch_json(url, timeout=timeout)
    jobs = payload.get("jobs", []) if isinstance(payload, dict) else []

    output: list[AtsPosting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = str(job.get("title", "")).strip()
        apply_url = str(job.get("url", "")).strip()
        if not title or not apply_url:
            continue
        output.append(
            AtsPosting(
                title=title,
                apply_url=apply_url,
                location=str(job.get("location", "")).strip(),
                posted_at=str(job.get("published") or job.get("created_at") or "").strip() or None,
                raw_payload=job,
            )
        )
    return output
