"""Personio XML job-feed client."""

from __future__ import annotations

from defusedxml import ElementTree as ET

from ultron.core.ats.clients.base import (
    UNCHECKED_ERRORS,
    AtsClientError,
    AtsHTTPClient,
    AtsPosting,
    locator_has_host,
    locator_url,
)


def _looks_like_personio_xml(payload: str) -> bool:
    normalized = payload.lstrip().lower()
    if not normalized:
        return False
    return normalized.startswith("<?xml") or "<workzag-jobs" in normalized or "<position" in normalized


def fetch_personio_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    """Fetch open postings from a Personio XML board feed."""
    fetch_text = client.request_text
    candidates = [
        locator_url(f"{board_identifier}.jobs.personio.de", "xml"),
        locator_url(f"{board_identifier}.jobs.personio.com", "xml"),
    ]
    if locator_has_host(board_identifier):
        # Only a host-like locator is tried as is: a bare slug ("https://acme/xml") is always rejected, and that
        # rejection would replace the real reason (e.g. a 404 for a removed board) as the reported error.
        candidates.append(locator_url(board_identifier, "xml"))
    payload = ""
    feed_url = ""
    last_exc: Exception | None = None
    for url in candidates:
        try:
            candidate = fetch_text(url, timeout=timeout)
            if not candidate:
                continue
            if not _looks_like_personio_xml(candidate):
                last_exc = AtsClientError(f"personio_invalid_payload={url}")
                continue
            payload = candidate
            try:
                root = ET.fromstring(payload)
                feed_url = url
                break
            except (ET.ParseError, ValueError) as exc:  # ValueError: defusedxml forbids DTD/entities
                last_exc = exc
                payload = ""
        except UNCHECKED_ERRORS:
            raise
        except Exception as exc:
            last_exc = exc
    if not payload:
        # A transport/XML-parse failure is an error; an empty feed is just no jobs.
        if last_exc is not None:
            raise AtsClientError(str(last_exc))
        return []

    # The Personio XML feed has no <url>/<jobUrl> element, so fall back to the
    # board's public job page (https://{slug}.jobs.personio.de/job/{id}).
    job_url_base = feed_url.rsplit("/xml", 1)[0].rstrip("/") if feed_url else ""

    out: list[AtsPosting] = []
    for job in root.findall(".//position"):
        title = (job.findtext("name") or "").strip()
        career_url = (job.findtext("url") or job.findtext("jobUrl") or "").strip()
        if not career_url:
            job_id = (job.findtext("id") or "").strip()
            if job_id and job_url_base:
                career_url = f"{job_url_base}/job/{job_id}"
        location = (job.findtext("office") or job.findtext("location") or "").strip()
        posted_at = (job.findtext("createdAt") or job.findtext("date") or "").strip() or None
        if title and career_url:
            out.append(AtsPosting(title=title, apply_url=career_url, location=location, posted_at=posted_at))
    return out
