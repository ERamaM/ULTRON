from __future__ import annotations

from defusedxml import ElementTree as ET

from ultron.core.ats.clients.base import AtsClientError, AtsHTTPClient, AtsPosting, locator_url
from ultron.infra.net import HttpError, RequestBudgetExceeded, URLRejected

_TT_LOC_NS = "{https://teamtailor.com/locations}"


def _parse_teamtailor_rss(xml_text: str) -> list[AtsPosting]:
    if not xml_text or "<rss" not in xml_text.lower() and "<item" not in xml_text.lower():
        return []
    try:
        root = ET.fromstring(xml_text)
    except (ET.ParseError, ValueError) as exc:
        raise AtsClientError(f"teamtailor_invalid_rss: {exc}") from exc
    out: list[AtsPosting] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        apply_url = (item.findtext("link") or "").strip()
        if not title or not apply_url:
            continue
        loc_parts: list[str] = []
        for loc in item.findall(f".//{_TT_LOC_NS}location"):
            parts = [
                (loc.findtext(f"{_TT_LOC_NS}{tag}") or "").strip()
                for tag in ("city", "country", "name")
            ]
            label = ", ".join(p for p in dict.fromkeys(parts) if p)
            if label and label not in loc_parts:
                loc_parts.append(label)
        remote_status = (item.findtext("remoteStatus") or "").strip().lower()
        remote_hint = (
            "Remote"
            if remote_status == "fully"
            else "Hybrid"
            if remote_status == "hybrid"
            else ""
        )
        location = "; ".join(loc_parts)
        posted_at = (item.findtext("pubDate") or "").strip() or None
        out.append(
            AtsPosting(
                title=title,
                apply_url=apply_url,
                location=location,
                remote_hint=remote_hint,
                posted_at=posted_at,
            )
        )
    return out


def fetch_teamtailor_jobs(
    board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient
) -> list[AtsPosting]:
    fetch_json = client.request_json
    url = locator_url(f"{board_identifier}.teamtailor.com", "api", "jobs")
    try:
        payload = fetch_json(url, timeout=timeout)
    except (RequestBudgetExceeded, URLRejected):
        raise
    except HttpError:
        rss_url = locator_url(f"{board_identifier}.teamtailor.com", "jobs.rss")
        rss_text = client.request_text(rss_url, timeout=timeout)
        return _parse_teamtailor_rss(rss_text)
    jobs = payload.get("data", payload) if isinstance(payload, dict) else payload
    if not isinstance(jobs, list):
        jobs = []
    out: list[AtsPosting] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        attrs = job.get("attributes") if isinstance(job.get("attributes"), dict) else job
        title = str(attrs.get("title") or attrs.get("name") or "").strip()
        path = str(attrs.get("path") or attrs.get("careers_url") or attrs.get("url") or "").strip()
        apply_url = path if path.startswith("http") else locator_url(f"{board_identifier}.teamtailor.com", path)
        location = str(attrs.get("location") or attrs.get("location_name") or "").strip()
        posted_at = str(attrs.get("created_at") or attrs.get("published_at") or "").strip() or None
        if title and apply_url:
            out.append(
                AtsPosting(title=title, apply_url=apply_url, location=location, posted_at=posted_at, raw_payload=job)
            )
    return out
