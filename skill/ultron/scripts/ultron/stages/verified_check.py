"""``ultron fetch --check-verified``: compare the agent's verified finds with what the fetcher sees.

It fetches every verified ``keep`` and ``unsure`` find once and records whether the posting reads as open, closed,
failed or unusable. It changes no verdict and adds nothing to the run's postings: it only measures how often the
fetcher calls closed a posting the agent found open (specification 30).
"""

from __future__ import annotations

from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.extractors import parse_ats_page
from ultron.core.models import JobRawPage
from ultron.core.rules.hard_filters import is_open_status_ok
from ultron.infra.net import HostBlocked, HttpError, RequestBudgetExceeded, URLRejected
from ultron.stages.fetch import get_posting_page, is_closed_redirect, page_canonical_url, unusable_page_reason
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, client_for_run
from ultron.stages.scout_verified import EXPORTED_VERDICTS, load_verified
from ultron.storage import runs

CHECK_FILE = "verified_check.jsonl"
CLOSED_STATUSES = frozenset({404, 410})


class NotScoutRun(ValueError):
    """``--check-verified`` only applies to scout runs."""


def fetch_outcome(client, url: str) -> str:
    """``open``, ``closed``, ``failed`` or ``unusable`` for one posting URL."""
    try:
        status, final_url, html = get_posting_page(client, url)
    except (HostBlocked, HttpError, RequestBudgetExceeded, URLRejected):
        return "failed"
    if status in CLOSED_STATUSES or is_closed_redirect(url, final_url):
        return "closed"
    if status >= 400:
        return "failed"
    page = JobRawPage(url=url, final_url=final_url, status_code=status, html=html,
                      canonical_url=page_canonical_url(html, final_url))
    try:
        job = parse_ats_page(page)
    except (ValueError, KeyError, TypeError, AttributeError):
        return "unusable"
    job.domain = domain_of(normalize_url(final_url))
    if not job.title or unusable_page_reason(html, job):  # nothing that reads as a posting
        return "unusable"
    open_ok = is_open_status_ok(job.body_text, job.apply_available, job.is_open_hint, expired=job.valid_through_expired)
    return "open" if open_ok else "closed"


def check_verified(run_id: str) -> dict:
    """Fetch each verified ``keep``/``unsure`` find once and write ``verified_check.jsonl``; verdicts stay as they are."""
    run = runs.load_run(run_id)
    if runs.run_mode(run) != "scout":
        raise NotScoutRun("--check-verified only applies to scout runs")
    out_dir = runs.run_dir(run_id)
    targets = [row for row in load_verified(out_dir).values() if row["verdict"] in EXPORTED_VERDICTS]
    client = client_for_run(run, block_threshold=HOST_BLOCK_THRESHOLD)
    rows = [{"identity_key": row["identity_key"], "apply_url": row["apply_url"], "agent_verdict": row["verdict"],
             "fetch_outcome": fetch_outcome(client, row["apply_url"])} for row in targets]
    runs.write_jsonl(out_dir / CHECK_FILE, rows)
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["fetch_outcome"]] = counts.get(row["fetch_outcome"], 0) + 1
    return {"checked": len(rows), "outcomes": counts, "agent_open_fetch_closed": counts.get("closed", 0),
            "requests": client.requests_made, "check_file": str(out_dir / CHECK_FILE)}
