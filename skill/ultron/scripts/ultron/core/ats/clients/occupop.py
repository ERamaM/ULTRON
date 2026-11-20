"""Occupop (Cezanne Recruitment): an employer's public job list is one embeddable page on the vendor's API host.

The board is the employer's share token from the careers-page embed (``https://api.occupop.com/api/jobs-frame/<token>``).
One GET per board, no pagination: each table row gives the title, the posting link
(``https://api.occupop.com/shared/job/<ref>``) and the location. Only links of that form on the vendor's API host
are kept, and at most ``MAX_POSTINGS`` of them.

A posting link redirects to a script-only page on another host, so the fetch stage reads the posting from the
vendor's public JSON (``/api/integration/job/<ref>``) instead; :func:`occupop_job_from_detail` maps it.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import AtsClientError, AtsHTTPClient, AtsPosting
from ultron.core.models import ExtractedJob

MAX_POSTINGS = 500
MAX_BODY_CHARS = 30_000  # the posting text limit every other extractor applies
MAX_DESCRIPTION_HTML = 200_000  # parse at most this much description markup
API_HOST = "api.occupop.com"
# Share tokens are long, case-sensitive and alphanumeric; posting refs are a title slug plus a short code.
_TOKEN_RE = re.compile(r"[A-Za-z0-9]{8,128}", re.ASCII)
_JOB_PATH_RE = re.compile(r"/shared/job/([A-Za-z0-9](?:[A-Za-z0-9-]{0,198}[A-Za-z0-9])?)/?", re.ASCII)
_NO_VACANCIES_RE = re.compile(r"\bno live vacancies\b", re.IGNORECASE)
_WORKPLACE_LABELS ={"onsite": "On-site", "remote": "Remote", "hybrid": "Hybrid"}


def _clean(text: object) -> str:
    return " ".join(str(text or "").split())


def occupop_job_ref(url: str) -> str:
    """The posting ref of an ``https://api.occupop.com/shared/job/<ref>`` link, or ``""`` for any other URL."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    if parts.scheme not in {"http", "https"} or (parts.hostname or "").lower() != API_HOST:
        return ""
    match = _JOB_PATH_RE.fullmatch(parts.path or "")
    return match.group(1) if match else ""


def occupop_posting_url(ref: str) -> str:
    return f"https://{API_HOST}/shared/job/{ref}"


def occupop_detail_url(ref: str) -> str:
    return f"https://{API_HOST}/api/integration/job/{ref}"


def occupop_job_from_detail(payload: object, apply_url: str, *, company_hint: str = "") -> ExtractedJob | None:
    """The posting from the vendor's job JSON, or ``None`` when the job is gone or closed.

    Only the fields a posting needs are read; the payload also carries application-form data that is never kept.
    """
    job = payload.get("job") if isinstance(payload, dict) else None
    if not isinstance(job, dict) or not _clean(job.get("title")):
        return None
    if job.get("closed") is True or job.get("is_active") is False or job.get("status") is False:
        return None
    location = _clean(job.get("city_name"))
    country = _clean(job.get("country"))
    if country and country.casefold() not in location.casefold():
        location = f"{location}, {country}" if location else country
    workplace = _WORKPLACE_LABELS.get(re.sub(r"[^a-z]", "", str(job.get("workplace") or "").lower()), "")
    remote_text = " | ".join(part for part in (workplace, location) if part)
    hiring = job.get("hiring_company") if isinstance(job.get("hiring_company"), dict) else {}
    company = _clean(job.get("company_name") or hiring.get("name") or company_hint)
    # A tenant controls the description: bound the markup parsed and the text kept per posting.
    description = str(job.get("description") or "")[:MAX_DESCRIPTION_HTML]
    body = BeautifulSoup(description, "html.parser").get_text("\n")
    body_text = "\n".join(line.strip() for line in body.splitlines() if line.strip())[:MAX_BODY_CHARS]
    return ExtractedJob(
        apply_url=apply_url,
        domain=urlsplit(apply_url).netloc.lower(),
        title=_clean(job.get("title")),
        company=company,
        location=location,
        remote_text=remote_text,
        body_text=body_text,
        apply_available=True,
        posting_date=_clean(job.get("created_at")) or None,
    )


def fetch_occupop_jobs(board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient) -> list[AtsPosting]:
    token = str(board_identifier or "").strip()
    if not _TOKEN_RE.fullmatch(token):
        raise AtsClientError("invalid_board")  # the board is the employer's share token
    board_url = f"https://{API_HOST}/api/jobs-frame/{token}"
    html = client.request_text(board_url, timeout=timeout)
    soup = BeautifulSoup(html or "", "html.parser")
    table = soup.select_one(".searchable")
    if table is None:
        # An embed without openings answers a notice instead of the table (an unknown token answers HTTP 401).
        if _NO_VACANCIES_RE.search(soup.get_text(" ", strip=True)):
            return []
        # Not a job list (an error or marketing page): never report it as a verified empty board.
        raise AtsClientError("unparseable_board")
    out: list[AtsPosting] = []
    seen: set[str] = set()
    for row in table.select("tr"):
        link = row.select_one(".title a[href]") or row.select_one("a[href]")
        if link is None:
            continue
        ref = occupop_job_ref(urljoin(board_url, str(link.get("href") or "")))
        title = _clean(link.get_text(" ", strip=True))
        if not ref or not title:
            continue
        apply_url = occupop_posting_url(ref)
        if apply_url in seen:
            continue
        seen.add(apply_url)
        location_el = row.select_one(".location")
        location = _clean(location_el.get_text(" ", strip=True)) if location_el is not None else ""
        out.append(AtsPosting(title=title, apply_url=apply_url, location=location))
        if len(out) >= MAX_POSTINGS:
            break
    return out
