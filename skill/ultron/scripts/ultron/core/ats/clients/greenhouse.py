"""Greenhouse job-board API client with US/EU endpoint fallback."""

from __future__ import annotations

import html
import logging
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import UNCHECKED_ERRORS, AtsHTTPClient, AtsPosting
from ultron.core.ats.vendors import greenhouse_job_url
from ultron.infra.net import URLRejected

logger = logging.getLogger(__name__)


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


def fetch_greenhouse_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient, include_content: bool = False,
) -> list[AtsPosting]:
    """Fetch open postings from a Greenhouse job board; ``include_content`` also returns each posting's text.

    A board whose listing with text fails (a large board can exceed the response size limit) is listed again
    without text, so its postings are never lost.
    """
    if include_content:
        try:
            return _fetch(board_identifier, timeout, client, include_content=True)
        except UNCHECKED_ERRORS:
            raise
        except Exception as exc:
            logger.debug("greenhouse board %s: listing with text failed (%s), listing without it",
                         board_identifier, type(exc).__name__)
    return _fetch(board_identifier, timeout, client, include_content=False)


def _fetch(board_identifier: str, timeout: int, client: AtsHTTPClient, *, include_content: bool) -> list[AtsPosting]:
    fetch_json = client.request_json
    payload = None
    used_eu = False
    last_exc: Exception | None = None
    for host, is_eu in (("boards-api.greenhouse.io", False), ("boards-api.eu.greenhouse.io", True)):
        url = f"https://{host}/v1/boards/{board_identifier}/jobs?content={'true' if include_content else 'false'}"
        try:
            payload = fetch_json(url, timeout=timeout)
            used_eu = is_eu
            break
        except UNCHECKED_ERRORS:
            raise
        except URLRejected:
            # A policy or DNS rejection of the EU fallback (its API host may not resolve) must not replace the
            # primary host's error: a board removed from Greenhouse would be reported as dns_resolution_failed.
            if is_eu and last_exc is not None:
                break
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
        title = str(job.get("title") or "").strip()
        apply_url = _canonical_greenhouse_url(board_identifier, job, eu=used_eu)
        if not title or not apply_url:
            continue
        location = ""
        location_payload = job.get("location")
        if isinstance(location_payload, dict):
            location = str(location_payload.get("name") or "").strip()
        posted_at = str(job.get("first_published") or job.get("created_at") or job.get("updated_at") or "").strip() or None
        output.append(
            AtsPosting(
                title=title,
                apply_url=apply_url,
                location=location,
                posted_at=posted_at,
                raw_payload={key: value for key, value in job.items() if key != "content"},
                description=_content_text(job.get("content")),
            )
        )
    return output


def _content_text(content: object) -> str:
    """The plain text of a Greenhouse posting's ``content`` (escaped HTML), or ``""``."""
    if not isinstance(content, str) or not content:
        return ""
    return " ".join(BeautifulSoup(html.unescape(content), "html.parser").get_text(" ").split())
