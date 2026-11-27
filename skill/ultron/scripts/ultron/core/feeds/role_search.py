"""Public job APIs that take a role query: what ``ultron search`` asks beyond the boards ULTRON knows.

Keyless sources (Remotive, Himalayas, Jobicy) accept a keyword. Keyed sources (Adzuna, Reed) are used only when the
user sets their keys in the environment (Adzuna also needs ``ADZUNA_COUNTRY``, such as ``es`` or ``gb``). Every row keeps its source's label and its own posting link, as the
sources' terms ask, and responses are cached by the caller for ``ttl_hours`` so ULTRON stays within each source's
request guidance. API keys are never put in error records.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlencode

from ultron.core.text_clean import clean_text

ADZUNA_COUNTRIES = frozenset({"gb", "us", "at", "au", "be", "br", "ca", "ch", "de", "es", "fr", "in", "it", "mx",
                              "nl", "nz", "pl", "sg", "za"})
PAGE_SIZE = 50


@dataclass(frozen=True)
class SourceRequest:
    """One request to a role source: its URL, optional headers and the host it may reach."""

    url: str
    host: str
    headers: dict[str, str] | None = None


@dataclass(frozen=True)
class RoleSource:
    """A public job API that takes a role query."""

    name: str
    label: str
    ttl_hours: float
    keyed: bool
    build: Callable[[str, str], SourceRequest | None]
    parse: Callable[[object], list[dict]]


def _row(source: str, title, company, url, location="", description="", posted="") -> dict | None:
    link = str(url or "").strip()
    if not link.startswith(("https://", "http://")):
        return None
    return {"title": clean_text(title), "company_name": clean_text(company), "url": link,
            "location": clean_text(location), "description": clean_text(description)[:2000],
            "posted": str(posted or "")[:10], "source": source}


def _jobs(payload: object, key: str = "jobs") -> list[dict]:
    items = payload.get(key) if isinstance(payload, dict) else payload
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _remotive_request(query: str, _country: str) -> SourceRequest:
    params = urlencode({"search": query, "limit": PAGE_SIZE})
    return SourceRequest(f"https://remotive.com/api/remote-jobs?{params}", "remotive.com")


def _remotive_rows(payload: object) -> list[dict]:
    return [r for item in _jobs(payload) if (r := _row(
        "remotive", item.get("title"), item.get("company_name"), item.get("url"),
        item.get("candidate_required_location"), item.get("description"), item.get("publication_date")))]


def _himalayas_request(query: str, _country: str) -> SourceRequest:
    return SourceRequest(f"https://himalayas.app/jobs/api/search?{urlencode({'q': query, 'page': 1})}", "himalayas.app")


def _himalayas_rows(payload: object) -> list[dict]:
    rows = []
    for item in _jobs(payload):
        places = item.get("locationRestrictions")
        location = ", ".join(str(p) for p in places if p) if isinstance(places, list) else ""
        posted = item.get("pubDate")
        if isinstance(posted, int | float):
            try:
                posted = datetime.fromtimestamp(posted, UTC).date().isoformat()
            except (OverflowError, OSError, ValueError):
                posted = None
        if r := _row("himalayas", item.get("title"), item.get("companyName"),
                     item.get("applicationLink") or item.get("guid"), location or "Worldwide",
                     item.get("excerpt") or item.get("description"), posted):
            rows.append(r)
    return rows


def _jobicy_request(query: str, _country: str) -> SourceRequest:
    return SourceRequest(f"https://jobicy.com/api/jobs?{urlencode({'tag': query, 'count': PAGE_SIZE})}", "jobicy.com")


def _jobicy_rows(payload: object) -> list[dict]:
    return [r for item in _jobs(payload) if (r := _row(
        "jobicy", item.get("jobTitle"), item.get("companyName"), item.get("url"), item.get("jobGeo"),
        item.get("jobExcerpt") or item.get("jobDescription"), item.get("pubDate")))]


def _adzuna_request(query: str, country: str) -> SourceRequest | None:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID", ""), os.environ.get("ADZUNA_APP_KEY", "")
    country = (country or os.environ.get("ADZUNA_COUNTRY") or "").lower()
    if not app_id or not app_key or country not in ADZUNA_COUNTRIES:
        return None
    params = urlencode({"app_id": app_id, "app_key": app_key, "what": query, "results_per_page": PAGE_SIZE})
    return SourceRequest(f"https://api.adzuna.com/v1/api/jobs/{country}/search/1?{params}", "api.adzuna.com")


def _adzuna_rows(payload: object) -> list[dict]:
    rows = []
    for item in _jobs(payload, "results"):
        company = item.get("company") if isinstance(item.get("company"), dict) else {}
        place = item.get("location") if isinstance(item.get("location"), dict) else {}
        if r := _row("adzuna", item.get("title"), company.get("display_name"), item.get("redirect_url"),
                     place.get("display_name"), item.get("description"), item.get("created")):
            rows.append(r)
    return rows


def _reed_request(query: str, _country: str) -> SourceRequest | None:
    key = os.environ.get("REED_API_KEY", "")
    if not key:
        return None
    token = base64.b64encode(f"{key}:".encode()).decode("ascii")
    url = f"https://www.reed.co.uk/api/1.0/search?{urlencode({'keywords': query, 'resultsToTake': PAGE_SIZE})}"
    return SourceRequest(url, "www.reed.co.uk", {"Authorization": f"Basic {token}"})


def _reed_rows(payload: object) -> list[dict]:
    rows = []
    for item in _jobs(payload, "results"):
        job_id = item.get("jobId") or item.get("Job Id")
        url = item.get("jobUrl") or (f"https://www.reed.co.uk/jobs/{job_id}" if job_id else "")
        if r := _row("reed", item.get("jobTitle") or item.get("Job Title"),
                     item.get("employerName") or item.get("Employer Name"), url,
                     item.get("locationName") or item.get("Location Name"),
                     item.get("jobDescription") or item.get("Description"), item.get("date")):
            rows.append(r)
    return rows


SOURCES: tuple[RoleSource, ...] = (
    RoleSource("remotive", "Remotive", 6.0, False, _remotive_request, _remotive_rows),
    RoleSource("himalayas", "Himalayas", 1.0, False, _himalayas_request, _himalayas_rows),
    RoleSource("jobicy", "Jobicy", 1.0, False, _jobicy_request, _jobicy_rows),
    RoleSource("adzuna", "Adzuna", 1.0, True, _adzuna_request, _adzuna_rows),
    RoleSource("reed", "Reed", 1.0, True, _reed_request, _reed_rows),
)

