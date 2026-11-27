"""schema.org ``JobPosting`` data embedded in pages: JSON-LD parsing, deadlines and posting identity."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ultron.core.extractors.text import collapse_whitespace as clean_text
from ultron.core.extractors.text import dedupe_join, looks_remote_workplace
from ultron.core.job_identity import canonical_job_identity
from ultron.core.json_walk import iter_json_dicts, load_json_or_none


def _json_load_loose(raw: str) -> object | None:
    text = str(raw or "").strip()
    if not text:
        return None
    return load_json_or_none(text)


def _iter_json_nodes(node: object) -> Iterable[dict]:
    return iter_json_dicts(node)


def _schema_type_leaf(value: object) -> str:
    token = str(value).strip().lower() if value is not None else ""
    cut = max(token.rfind("/"), token.rfind("#"))
    if cut < 0:
        return token
    return token[cut + 1 :]


def _iter_jobposting_nodes(soup: BeautifulSoup) -> Iterable[dict]:
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for script in scripts:
        raw = script.string or script.get_text(" ", strip=True)
        payload = _json_load_loose(raw)
        if payload is None:
            continue
        for node in _iter_json_nodes(payload):
            type_raw = node.get("@type")
            candidates = type_raw if isinstance(type_raw, list) else (type_raw,)
            if any(_schema_type_leaf(item) == "jobposting" for item in candidates):
                yield node


# Values some ATSs (iCIMS: "UNAVAILABLE") write into schema.org fields they have no data for.
_SCHEMA_PLACEHOLDERS = frozenset({"unavailable", "not available", "n/a", "na", "none", "null", "undefined", "-"})


def _schema_text(value: object) -> str:
    """Cleaned schema.org text; ``""`` for a placeholder that stands for a missing value."""
    text = clean_text(str(value or ""))
    return "" if text.casefold() in _SCHEMA_PLACEHOLDERS else text


def _extract_company_from_jobposting(job: dict) -> str:
    hiring_org = job.get("hiringOrganization")
    if isinstance(hiring_org, dict):
        return _schema_text(hiring_org.get("name"))
    return _schema_text(hiring_org)


def _extract_location_parts(value: object) -> list[str]:
    parts: list[str] = []
    if isinstance(value, dict):
        address = value.get("address") if isinstance(value.get("address"), dict) else value
        if isinstance(address, dict):
            for key in ("addressLocality", "addressRegion", "addressCountry", "streetAddress"):
                raw = address.get(key)
                # schema.org allows typed values such as {"@type": "Country", "name": "ES"}.
                if isinstance(raw, dict):
                    raw = raw.get("name")
                data = _schema_text(raw)
                if data:
                    parts.append(data)
        name = _schema_text(value.get("name"))
        if name:
            parts.append(name)
    elif isinstance(value, list):
        for item in value:
            parts.extend(_extract_location_parts(item))
    elif value and (data := _schema_text(value)):
        parts.append(data)
    return parts


def _structured_fields_from_node(job: dict) -> dict[str, str]:
    title = clean_text(str(job.get("title") or ""))
    company = _extract_company_from_jobposting(job)
    location_parts: list[str] = []
    location_parts.extend(_extract_location_parts(job.get("jobLocation")))
    location_parts.extend(_extract_location_parts(job.get("applicantLocationRequirements")))
    location = dedupe_join(location_parts, limit=3)

    remote_bits: list[str] = []
    location_type = clean_text(str(job.get("jobLocationType") or ""))
    if "telecommute" in location_type.lower():
        remote_bits.append("Remote")
    if looks_remote_workplace(location):
        remote_bits.append(location)
    remote_text = dedupe_join(remote_bits, limit=2)

    # datePosted is the publication date; validThrough is the application
    # deadline and must NOT be used as the posting date. It is kept separately
    # and only used to detect expired postings (see valid_through_expired).
    posting_date = clean_text(str(job.get("datePosted") or ""))
    updated_at = clean_text(str(job.get("dateModified") or ""))
    valid_through = job.get("validThrough")
    description = job.get("description")
    return {
        "title": title,
        "company": company,
        "location": location,
        "remote_text": remote_text,
        "posting_date": posting_date,
        "updated_at": updated_at,
        "valid_through": valid_through.strip() if isinstance(valid_through, str) else "",
        "description": description if isinstance(description, str) else "",
    }


# Dates before this year are placeholders (epoch zero, 1900-01-01), never real deadlines.
_MIN_VALID_THROUGH_YEAR = 2000


def _is_date_only(raw: str) -> bool:
    try:
        date.fromisoformat(raw)
    except ValueError:
        return False
    return True


# An RFC 3339 / ISO 8601 date-time is at most ~35 characters; longer values come from untrusted pages and are
# ignored before any string processing.
_MAX_VALID_THROUGH_CHARS = 64


def valid_through_expired(value: str, *, now: datetime | None = None) -> bool:
    """True only when a JSON-LD ``validThrough`` parses to a deadline in the past.

    A date-only value counts through the end of that UTC day. Naive date-times
    are read as UTC. Missing, unparseable or placeholder values never expire a
    posting: bad data must not drop a job.
    """
    if not isinstance(value, str) or len(value) > _MAX_VALID_THROUGH_CHARS:
        return False
    raw = value.strip()
    if not raw:
        return False
    if raw[-1] in "Zz":  # only a terminal UTC designator
        raw = raw[:-1] + "+00:00"
    try:
        deadline = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if deadline.year < _MIN_VALID_THROUGH_YEAR:
        return False
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=UTC)
    if _is_date_only(raw):
        try:
            deadline += timedelta(days=1)
        except OverflowError:
            return False
    return deadline <= (now or datetime.now(UTC))




_MAX_IDENTITY_URL_CHARS = 2048  # posting URLs come from untrusted pages; longer ones are never parsed


def _posting_identity(url: str) -> str:
    """Identity of the posting a URL points at; keeps job-identifying query parameters such as ``gh_jid``.

    An overlong URL gets no identity (``""``), so it never matches the page and its query is never parsed.
    """
    if len(url) > _MAX_IDENTITY_URL_CHARS:
        return ""
    try:
        return canonical_job_identity(url).identity_key
    except (ValueError, TypeError):
        return ""


# At most this many JobPosting node URLs are canonicalized per page. A page with more never yields deadline
# evidence (validThrough is ignored), so a hostile page cannot make identity matching slow, and an uncertain
# match never drops a posting.
_MAX_IDENTITY_CHECKS = 50


def extract_structured_fields(soup: BeautifulSoup, page_url: str | None = None) -> dict[str, str]:
    """Read structured facts from this posting or a JobPosting without a URL."""
    fallback: dict[str, str] = {}
    page_identity = _posting_identity(page_url) if page_url else ""
    identity_checks = 0
    for job in _iter_jobposting_nodes(soup):
        node_url = job.get("url")
        if page_url and isinstance(node_url, str) and node_url.strip():
            identity_checks += 1
            if page_identity and identity_checks <= _MAX_IDENTITY_CHECKS:
                try:
                    resolved = urljoin(page_url, node_url.strip())
                except ValueError:
                    continue
                if _posting_identity(resolved) == page_identity:
                    return _structured_fields_from_node(job)
            continue
        if not fallback.get("title"):
            fields = _structured_fields_from_node(job)
            if fields.get("title") or not fallback:
                fallback = fields
            if not page_url and fields.get("title"):
                return fields
    if identity_checks > _MAX_IDENTITY_CHECKS and fallback.get("valid_through"):
        fallback["valid_through"] = ""  # too many postings to tell which deadline belongs to the page
    return fallback

