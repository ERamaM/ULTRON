from __future__ import annotations

import logging
import re
from collections import deque
from urllib.parse import urlparse

from ultron.core.ats.clients.base import (
    UNCHECKED_ERRORS,
    AtsClientError,
    AtsHTTPClient,
    AtsPosting,
    locator_has_host,
    locator_url,
    refuse_browser_fallback,
)
from ultron.core.json_walk import iter_json_dicts, load_json_or_none
from ultron.core.rules.term_matching import normalize_text
from ultron.infra.net import HostBlocked, HttpError, RequestBudgetExceeded, URLRejected

logger = logging.getLogger(__name__)

NEXT_DATA_RE = re.compile(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL | re.IGNORECASE)


def _split_locator(locator: str) -> tuple[str, str]:
    clean = str(locator or "").strip()
    if not clean:
        return "", ""
    if clean.startswith(("http://", "https://")):
        parsed = urlparse(clean)
        return parsed.netloc.strip(), parsed.path.strip("/")
    clean = clean.strip("/")
    if "/" not in clean:
        return clean, ""
    host, path = clean.split("/", 1)
    return host, path.strip("/")


def _workday_apply_url(host: str, path: str, external_path: str) -> str:
    clean = str(external_path or "").strip()
    if not clean or not clean.startswith("/"):
        return clean
    clean_path = path.strip("/")
    if not clean_path:
        return locator_url(host, clean)
    site = clean_path.split("/")[-1]
    trimmed = clean.lstrip("/")
    if trimmed in {clean_path, site} or trimmed.startswith((f"{clean_path}/", f"{site}/")):
        return locator_url(host, clean)
    return locator_url(host, clean_path, clean)


def _extract_from_next_data(html: str, host: str, path: str = "") -> list[AtsPosting]:
    match = NEXT_DATA_RE.search(html)
    if not match:
        return []
    payload = load_json_or_none(match.group(1))
    if payload is None:
        return []

    out: list[AtsPosting] = []
    for item in iter_json_dicts(payload):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        raw_path = str(item.get("externalPath") or "").strip().replace("\\/", "/")
        apply_url = _workday_apply_url(host, path, raw_path)
        location = str(item.get("locationsText") or "").strip()
        if title and apply_url:
            out.append(AtsPosting(title=title, apply_url=apply_url, location=location))
    deduped: list[AtsPosting] = []
    seen: set[str] = set()
    for posting in out:
        if posting.apply_url in seen:
            continue
        seen.add(posting.apply_url)
        deduped.append(posting)
    return deduped


def _country_facets(payload: dict, preferred_locations: tuple[str, ...]) -> dict[str, list[str]]:
    preferred = {normalize_text(location.strip()) for location in preferred_locations}
    groups = [group for group in payload.get("facets") or [] if isinstance(group, dict)]
    groups.extend(
        child for group in tuple(groups) if group.get("facetParameter") == "locationMainGroup"
        for child in group.get("values") or [] if isinstance(child, dict)
    )
    for group in groups:
        if group.get("facetParameter") == "locationCountry":
            ids = [
                row["id"] for row in group.get("values") or []
                if isinstance(row, dict) and row.get("id") and normalize_text(row.get("descriptor") or "") in preferred
            ]
            return {"locationCountry": ids} if ids else {}
    return {}


def fetch_workday_jobs(
    board_identifier: str,
    timeout: int = 20,
    *,
    client: AtsHTTPClient,
    max_pages: int | None = None,
    preferred_locations: tuple[str, ...] = (),
) -> list[AtsPosting]:
    fetch_json = client.request_json
    fetch_text = client.request_text
    if not locator_has_host(board_identifier):
        raise AtsClientError(f"workday_locator_invalid={board_identifier}")
    host, path = _split_locator(board_identifier)
    if not host:
        raise AtsClientError(f"workday_locator_missing={board_identifier}")
    base_url = locator_url(host, path)
    api_path = f"wday/cxs/{host.split('.')[0]}/{path.split('/')[-1] if path else 'External'}"
    api_url = locator_url(host, api_path, "jobs")
    _WD_PAGE_LIMIT = 20
    _WD_MAX_PAGES = 100  # Safety cap: max 2000 jobs per board
    page_cap = _WD_MAX_PAGES if max_pages is None else max(1, min(_WD_MAX_PAGES, int(max_pages)))
    out: list[AtsPosting] = []
    seen: set[str] = set()
    pages: deque[tuple[dict[str, list[str]], int, int | None]] = deque([({}, 0, None)])
    initial_cxs_error: HttpError | None = None
    for page in range(page_cap):
        if not pages:
            break
        facets, offset, total = pages.popleft()
        try:
            payload = fetch_json(
                api_url,
                method="POST",
                timeout=timeout,
                json_body={"appliedFacets": facets, "limit": _WD_PAGE_LIMIT, "offset": offset, "searchText": ""},
            )
        except RequestBudgetExceeded as exc:
            if exc.scope in {"host", "vendor"} and out:
                logger.warning(
                    "workday pagination stopped on %s budget board=%s page=%d; returning partial results (%d postings)",
                    exc.scope,
                    board_identifier,
                    page,
                    len(out),
                )
                return out
            raise
        except HostBlocked:
            # The host or the whole vendor is blocked for this run: keep what was collected, no HTML fallback.
            if out:
                return out
            raise
        except HttpError as exc:
            if page == 0:
                initial_cxs_error = exc
                break
            if facets and exc.status_code in {400, 404, 422}:
                logger.debug("workday country filter rejected board=%s status=%s", board_identifier, exc.status_code)
                continue
            logger.warning(
                "workday pagination aborted board=%s page=%d offset=%d error=%s; returning partial results (%d postings)",
                board_identifier,
                page,
                offset,
                exc,
                len(out),
            )
            break
        except URLRejected:
            raise
        except Exception as exc:
            logger.warning(
                "workday pagination aborted board=%s page=%d offset=%d error=%s; returning partial results (%d postings)",
                board_identifier,
                page,
                offset,
                exc,
                len(out),
            )
            break
        jobs = payload.get("jobPostings", []) if isinstance(payload, dict) else []
        if not jobs:
            continue
        # Workday can report total=0 on later pages even when there are more jobs.
        if offset == 0 and isinstance(payload.get("total"), int) and payload["total"] > 0:
            total = payload["total"]
        next_offset = offset + _WD_PAGE_LIMIT
        if len(jobs) >= _WD_PAGE_LIMIT and (total is None or next_offset < total):
            if page == 0 and preferred_locations:
                country_facets = _country_facets(payload, preferred_locations)
                if country_facets:
                    pages.append((country_facets, 0, None))
            pages.append((facets, next_offset, total))
        for job in jobs:
            if not isinstance(job, dict):
                continue
            title = str(job.get("title") or "").strip()
            apply_url = _workday_apply_url(host, path, str(job.get("externalPath") or ""))
            location = str(job.get("locationsText") or "").strip()
            posted_at = str(job.get("postedOn") or "").strip() or None
            if title and apply_url and apply_url not in seen:
                seen.add(apply_url)
                out.append(
                    AtsPosting(
                        title=title, apply_url=apply_url, location=location, posted_at=posted_at, raw_payload=job
                    )
                )
    else:
        if pages and max_pages is None:
            logger.warning(
                "workday pagination truncated at safety cap board=%s pages=%d postings=%d",
                board_identifier,
                page_cap,
                len(out),
            )
    if out:
        return out

    html = ""
    try:
        html = fetch_text(base_url, timeout=timeout)
    except UNCHECKED_ERRORS:
        raise
    except Exception:
        if initial_cxs_error is not None:
            raise initial_cxs_error
        refuse_browser_fallback()
    out = _extract_from_next_data(html, host, path)
    if not out and initial_cxs_error is not None:
        raise initial_cxs_error
    # No postings extracted: an empty board, not an error.
    return out
