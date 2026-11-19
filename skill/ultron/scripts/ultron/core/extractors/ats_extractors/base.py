from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ultron.core.ats.vendors import canonical_domain_for_vendor, match_vendor_by_fingerprint
from ultron.core.extractors.ats_extractors.posting_date import posting_date_from_text
from ultron.core.json_walk import iter_json_dicts, load_json_or_none
from ultron.core.models import ExtractedJob, JobRawPage
from ultron.core.rules.geo_eu import EXTRACTOR_COMPENSATION_OVERRIDE_TERMS, EXTRACTOR_LOCATION_MARKERS
from ultron.core.rules.hard_filters_data import STRONG_REMOTE_POSITIVE_TERMS

_LOCATION_MARKER_RE = re.compile(r"\b(?:location|based in|located in|" + "|".join(EXTRACTOR_LOCATION_MARKERS) + r")\b")
REMOTE_HINT_TERMS = ("remote", "remoto", "telecommute", "distributed", "work from home")
# Explicit workplace-conflict terms (hybrid/onsite). A bare "office" mention is NOT a
# workplace signal — incidental lines like "Optional office in Berlin" must not match.
REMOTE_CONFLICT_TERMS = ("hybrid", "on-site", "onsite", "in-office")
APPLY_HINT_TERMS = ("apply", "submit application", "apply now", "send application", "accepting applications")
OPEN_CLOSED_TERMS = (
    "position filled",
    "job expired",
    "no longer accepting",
    "this position is closed",
    "this role is closed",
    "posting is closed",
    "role has been filled",
    "job has expired",
    "link expired",
)
LOCATION_LABEL_TERMS = ("location", "ubicación", "ubicacion", "where", "location:")
REMOTE_TECH_CONTEXT = (
    "remote monitoring",
    "remote access",
    "remote procedure",
    "remote sensing",
    "remote desktop",
    "remote connection",
    "distributed systems",
    "distributed computing",
    "distributed database",
    "distributed architecture",
)
_STRONG_REMOTE_PHRASES = (*STRONG_REMOTE_POSITIVE_TERMS, "remote position", "work from home")
GENERIC_COMPANY_TERMS = (
    "boards",
    "jobs",
    "job",
    "apply",
    "careers",
    "career",
)
GENERIC_TITLE_TERMS = (
    "jobs",
    "job",
    "apply",
    "careers",
    "career",
    "open roles",
)
GENERIC_HOST_LABELS = (
    "www",
    "boards",
    "jobs",
    "job",
    "apply",
    "careers",
    "career",
    "careersite",
)
ATS_VENDOR_HOST_LABELS = (
    "greenhouse",
    "greenhouseio",
    "lever",
    "ashbyhq",
    "workable",
    "personio",
    "recruitee",
    "homerun",
    "smartrecruiters",
    "teamtailor",
    "jobvite",
    "bamboohr",
    "breezy",
    "applytojob",
    "jazzhr",
    "factorialhr",
    "factorial",
    "pinpointhq",
    "pinpoint",
    "comeet",
    "workday",
    "myworkdayjobs",
    "icims",
    "taleo",
)
LOCATION_VERB_HINTS = (
    "build",
    "design",
    "develop",
    "experience",
    "responsibilities",
    "requirements",
    "you will",
    "we are looking",
)
LOCATION_COMPENSATION_HINTS = (
    "base pay range",
    "salary range",
    "compensation range",
    "pay range",
    "pay transparency",
    "for us locations",
    "for u.s. locations",
    "cash compensation",
    "total compensation",
    "equity",
    "stock options",
)
LOCATION_COMPENSATION_OVERRIDE_RE = re.compile(
    r"\b(remote|remoto|" + "|".join(EXTRACTOR_COMPENSATION_OVERRIDE_TERMS) + r")\b"
)
SOURCE_PRIORITY = {
    "ats_hint": 95,
    "jsonld": 90,
    "meta_og": 75,
    "meta_twitter": 72,
    "selector": 70,
    "h1": 60,
    "title_tag": 45,
    "line_heuristic": 35,
    "domain_fallback": 20,
    "unknown": 10,
}


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _same_text(left: str, right: str) -> bool:
    return _clean_text(left).casefold() == _clean_text(right).casefold()


def _looks_remote(text: str) -> bool:
    """True only for genuine remote-work hints (no hybrid/onsite conflict terms)."""
    low = _clean_text(text).lower()
    if not low:
        return False
    if any(token in low for token in REMOTE_TECH_CONTEXT):
        return False
    return any(token in low for token in REMOTE_HINT_TERMS)


def _looks_remote_workplace(text: str) -> bool:
    """True for remote hints OR explicit hybrid/onsite conflict terms.

    Used where workplace-policy lines must be captured so downstream filters can
    reject hybrids; a bare "office" mention is intentionally not a signal.
    """
    low = _clean_text(text).lower()
    if not low:
        return False
    if any(phrase in low for phrase in _STRONG_REMOTE_PHRASES):
        return True
    if _looks_remote(text):
        return True
    return any(token in low for token in REMOTE_CONFLICT_TERMS)


def _dedupe_join(values: Iterable[str], *, limit: int = 4) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = _clean_text(value)
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(clean)
        if len(output) >= limit:
            break
    return " | ".join(output)


def _pick_best_candidate_with_source(
    candidates: Iterable[tuple[str, str]],
    *,
    page_url: str,
    field: str,
) -> tuple[str, str]:
    best_value = ""
    best_source = "unknown"
    best_score = -10_000
    seen: set[tuple[str, str]] = set()
    for value_raw, source in candidates:
        value = _clean_text(value_raw)
        if not value:
            continue
        key = (value.lower(), source)
        if key in seen:
            continue
        seen.add(key)

        score = SOURCE_PRIORITY.get(source, SOURCE_PRIORITY["unknown"])
        if field == "title":
            if _looks_generic_title(value, page_url):
                score -= 50
            if len(value) > 120:
                score -= 10
            if len(value) < 4:
                score -= 20
        elif field == "company":
            if _looks_generic_company(value, page_url):
                score -= 60
            if len(value) > 90:
                score -= 10
        elif field == "location":
            if not _is_location_like_line(value):
                score -= 25
            if len(value) > 80:
                score -= 12
        elif field == "remote":
            if not _looks_remote_workplace(value):
                score -= 30
            if len(value) > 100:
                score -= 8

        if score > best_score:
            best_score = score
            best_value = value
            best_source = source
    return best_value, best_source


def _pick_best_candidate(
    candidates: Iterable[tuple[str, str]],
    *,
    page_url: str,
    field: str,
) -> str:
    value, _ = _pick_best_candidate_with_source(candidates, page_url=page_url, field=field)
    return value


def _node_text(node) -> str:
    value = _clean_text(node.get_text(" ", strip=True))
    if value:
        return value
    # <img> elements (and wrappers containing only an <img>) carry no text;
    # fall back to the image's alt attribute.
    if node.name == "img":
        return _clean_text(str(node.get("alt") or ""))
    img = node.find("img") if hasattr(node, "find") else None
    if img is not None:
        return _clean_text(str(img.get("alt") or ""))
    return ""


def _first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> str:
    for selector in selectors:
        node = soup.select_one(selector)
        if not node:
            continue
        value = _node_text(node)
        if value:
            return value
    return ""


def _iter_selector_texts(soup: BeautifulSoup, selectors: Iterable[str]) -> Iterable[str]:
    seen: set[str] = set()
    for selector in selectors:
        for node in soup.select(selector):
            value = _clean_text(node.get_text(" ", strip=True))
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            yield value


def _meta_content(soup: BeautifulSoup, *, property_name: str | None = None, name: str | None = None) -> str:
    attrs: dict[str, str] = {}
    if property_name:
        attrs["property"] = property_name
    if name:
        attrs["name"] = name
    if not attrs:
        return ""
    node = soup.find("meta", attrs=attrs)
    if not node:
        return ""
    return _clean_text(str(node.get("content") or ""))


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


def _extract_company_from_jobposting(job: dict) -> str:
    hiring_org = job.get("hiringOrganization")
    if isinstance(hiring_org, dict):
        return _clean_text(str(hiring_org.get("name") or ""))
    return _clean_text(str(hiring_org or ""))


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
                data = _clean_text(str(raw or ""))
                if data:
                    parts.append(data)
        name = _clean_text(str(value.get("name") or ""))
        if name:
            parts.append(name)
    elif isinstance(value, list):
        for item in value:
            parts.extend(_extract_location_parts(item))
    elif value:
        parts.append(_clean_text(str(value)))
    return parts


def _structured_fields_from_node(job: dict) -> dict[str, str]:
    title = _clean_text(str(job.get("title") or ""))
    company = _extract_company_from_jobposting(job)
    location_parts: list[str] = []
    location_parts.extend(_extract_location_parts(job.get("jobLocation")))
    location_parts.extend(_extract_location_parts(job.get("applicantLocationRequirements")))
    location = _dedupe_join(location_parts, limit=3)

    remote_bits: list[str] = []
    location_type = _clean_text(str(job.get("jobLocationType") or ""))
    if "telecommute" in location_type.lower():
        remote_bits.append("Remote")
    if _looks_remote_workplace(location):
        remote_bits.append(location)
    remote_text = _dedupe_join(remote_bits, limit=2)

    # datePosted is the publication date; validThrough is the application
    # deadline and must NOT be used as the posting date. It is kept separately
    # and only used to detect expired postings (see _valid_through_expired).
    posting_date = _clean_text(str(job.get("datePosted") or job.get("dateModified") or ""))
    valid_through = job.get("validThrough")
    description = job.get("description")
    return {
        "title": title,
        "company": company,
        "location": location,
        "remote_text": remote_text,
        "posting_date": posting_date,
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


def _valid_through_expired(value: str, *, now: datetime | None = None) -> bool:
    """True only when a JSON-LD ``validThrough`` parses to a deadline in the past.

    A date-only value counts through the end of that UTC day. Naive date-times
    are read as UTC. Missing, unparseable or placeholder values never expire a
    posting: bad data must not drop a job.
    """
    raw = str(value or "").strip()
    if not raw:
        return False
    try:
        deadline = datetime.fromisoformat(raw.replace("Z", "+00:00").replace("z", "+00:00"))
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


def _comparable_page_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def _extract_structured_fields(soup: BeautifulSoup, page_url: str | None = None) -> dict[str, str]:
    """Pick the JobPosting of the evaluated page; fall back to the first titled one."""
    first_fields: dict[str, str] | None = None
    page_key = _comparable_page_url(page_url or "")
    for job in _iter_jobposting_nodes(soup):
        fields = _structured_fields_from_node(job)
        node_url = job.get("url")
        if page_key and isinstance(node_url, str) and _comparable_page_url(node_url) == page_key:
            return fields
        if page_key and isinstance(node_url, str) and node_url.strip():
            # A node that names another posting (e.g. a recommendation) may still fill in fields, but its
            # deadline is not evidence that this page has expired.
            fields = {**fields, "valid_through": ""}
        if fields.get("title"):
            if not page_key:
                return fields
            if first_fields is None or not first_fields.get("title"):
                first_fields = fields
            continue
        if first_fields is None:
            first_fields = fields
    return first_fields or {}


def extract_text_lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _is_location_like_line(line: str) -> bool:
    clean = _clean_text(line)
    if not clean:
        return False
    if len(clean) > 80:
        return False
    low = clean.lower()
    if any(token in low for token in LOCATION_VERB_HINTS):
        return False
    if clean.count(".") >= 2:
        return False
    has_brief_comma_location = "," in clean and len(clean.split()) <= 8 and bool(re.search(r"[a-z].*,\s*[a-z]", low))
    has_strong_location_signal = (
        _looks_remote_workplace(clean)
        or bool(LOCATION_COMPENSATION_OVERRIDE_RE.search(low))
        or has_brief_comma_location
    )
    if _looks_compensation_line(low) and not has_strong_location_signal:
        return False
    if _looks_remote_workplace(clean):
        return True
    if _LOCATION_MARKER_RE.search(low):
        return True
    return bool(has_brief_comma_location)


def _looks_compensation_line(low: str) -> bool:
    if any(token in low for token in LOCATION_COMPENSATION_HINTS):
        return True
    has_currency_symbol = any(symbol in low for symbol in ("$", "€", "£"))
    has_currency_code = any(code in low for code in (" usd", " eur", " gbp", " cad", " aud"))
    has_amount = bool(re.search(r"\d[\d,.]*", low))
    return bool(has_amount and (has_currency_symbol or has_currency_code))


def _extract_location_from_lines(lines: list[str]) -> str:
    # Prefer explicit location labels; fall back to short remote/location-like lines.
    labeled = [ln for ln in lines if any(token in ln.lower() for token in LOCATION_LABEL_TERMS)]
    for line in labeled:
        clean = _clean_text(line)
        if _is_location_like_line(clean):
            return clean
    for line in lines:
        clean = _clean_text(line)
        if _is_location_like_line(clean):
            return clean
    return ""


def _extract_remote_lines(lines: list[str]) -> str:
    remote_lines = [ln for ln in lines if _looks_remote_workplace(ln) and len(_clean_text(ln)) <= 100]
    return _dedupe_join(remote_lines, limit=4)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _host_labels(url: str) -> list[str]:
    host = urlparse(url).netloc.lower()
    return [part for part in host.split(".") if part]


def _looks_domain_placeholder(value: str, page_url: str) -> bool:
    clean = _clean_text(value)
    if not clean:
        return True
    key = _normalized_key(clean)
    if not key:
        return True
    labels = _host_labels(page_url)[:-1]
    host = urlparse(page_url).netloc.lower()
    placeholder_label_keys = {_normalized_key(item) for item in (*GENERIC_HOST_LABELS, *ATS_VENDOR_HOST_LABELS)}
    host_keys = {_normalized_key(host)}
    host_keys.update(_normalized_key(label) for label in labels if _normalized_key(label) in placeholder_label_keys)
    return key in host_keys


def _looks_generic_company(value: str, page_url: str) -> bool:
    clean = _clean_text(value)
    if not clean:
        return True
    low = clean.lower()
    key = _normalized_key(clean)
    if low in GENERIC_COMPANY_TERMS or key in {"jobboard", "jobboards", "careerjobs", "careersjobs"}:
        return True
    if key in {_normalized_key(item) for item in GENERIC_HOST_LABELS + ATS_VENDOR_HOST_LABELS}:
        return True
    return _looks_domain_placeholder(clean, page_url)


def _looks_generic_title(value: str, page_url: str) -> bool:
    clean = _clean_text(value)
    if not clean:
        return True
    if clean.lower() in GENERIC_TITLE_TERMS:
        return True
    return _looks_domain_placeholder(clean, page_url)


def _fallback_company_from_domain(page_url: str) -> str:
    for label in _host_labels(page_url)[:-1]:
        key = _normalized_key(label)
        if not key:
            continue
        if key in {_normalized_key(item) for item in GENERIC_HOST_LABELS + ATS_VENDOR_HOST_LABELS}:
            continue
        return label.replace("-", " ").replace("_", " ").title()
    return ""


def _clean_ats_hints(ats_hints: dict[str, str] | None) -> dict[str, str]:
    if not ats_hints:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in ats_hints.items():
        text = _clean_text(str(value or ""))
        if text:
            cleaned[key] = text
    return cleaned


def _apply_ats_hints(extracted: ExtractedJob, page: JobRawPage, ats_hints: dict[str, str] | None) -> ExtractedJob:
    hints = _clean_ats_hints(ats_hints)
    if not hints:
        return extracted

    updates: dict[str, str] = {}
    title_hint = hints.get("title", "")
    if title_hint and (not extracted.title or _looks_generic_title(extracted.title, page.final_url)):
        updates["title"] = title_hint

    company_hint = hints.get("company", "")
    if company_hint and (not extracted.company or _looks_generic_company(extracted.company, page.final_url)):
        updates["company"] = company_hint

    location_hint = hints.get("location", "")
    # ATS hints are the highest-priority location source.  Location
    # heuristics are permissive enough to produce a non-empty but unrelated
    # line, so retaining any extracted value can hide a trusted board hint.
    if location_hint:
        updates["location"] = location_hint

    remote_hint = hints.get("remote_hint", "")
    if remote_hint and _looks_remote_workplace(remote_hint):
        remote_text = _dedupe_join([extracted.remote_text, remote_hint], limit=4)
        if remote_text and remote_text != extracted.remote_text:
            updates["remote_text"] = remote_text

    if not updates:
        return extracted
    return extracted.model_copy(update=updates)


def _parse_generic_with_provenance(
    page: JobRawPage, ats_hints: dict[str, str] | None = None, *, use_jsonld_description: bool = False,
) -> tuple[ExtractedJob, dict[str, str]]:
    """Like parse_generic, but also returns the winning source per field.

    The provenance dict maps field name ("title"/"company"/"location"/"remote_text")
    to a SOURCE_PRIORITY key (e.g. "jsonld", "h1", "line_heuristic", "ats_hint"),
    so callers re-scoring these values keep their true priority.
    """
    soup = BeautifulSoup(page.html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = extract_text_lines(text)
    structured = _extract_structured_fields(soup, page.final_url)

    h1_text = ""
    h1 = soup.find("h1")
    if h1:
        h1_text = h1.get_text(" ", strip=True)
    title_candidates = [
        (structured.get("title", ""), "jsonld"),
        (_meta_content(soup, property_name="og:title"), "meta_og"),
        (
            _meta_content(soup, name="twitter:title") or _meta_content(soup, property_name="twitter:title"),
            "meta_twitter",
        ),
        (h1_text, "h1"),
        ((soup.title.get_text(" ", strip=True) if soup.title else ""), "title_tag"),
    ]
    title, title_source = _pick_best_candidate_with_source(title_candidates, page_url=page.final_url, field="title")

    company_candidates = [
        (structured.get("company", ""), "jsonld"),
        (_meta_content(soup, property_name="og:site_name"), "meta_og"),
        (_fallback_company_from_domain(page.final_url), "domain_fallback"),
    ]
    company, company_source = _pick_best_candidate_with_source(
        company_candidates, page_url=page.final_url, field="company"
    )

    location_candidates = [
        (structured.get("location", ""), "jsonld"),
        (_extract_location_from_lines(lines), "line_heuristic"),
    ]
    location, location_source = _pick_best_candidate_with_source(
        location_candidates, page_url=page.final_url, field="location"
    )
    location = _clean_text(location)

    remote_candidates = [
        (structured.get("remote_text", ""), "jsonld"),
        (_extract_remote_lines(lines), "line_heuristic"),
    ]
    remote_text, remote_source = _pick_best_candidate_with_source(
        remote_candidates, page_url=page.final_url, field="remote"
    )
    if not remote_text and _looks_remote_workplace(location):
        remote_text = location
        remote_source = location_source

    text_low = text.lower()
    apply_available = any(token in text_low for token in APPLY_HINT_TERMS)

    closed = any(token in text_low for token in OPEN_CLOSED_TERMS)

    posting_date = structured.get("posting_date", "") or None
    if not posting_date:
        posting_date = posting_date_from_text(text)

    body_text = text
    if use_jsonld_description and (not text or "you need to enable javascript to run this app" in text_low):
        description = structured.get("description", "")
        body_text = BeautifulSoup(description, "html.parser").get_text("\n", strip=True) or text

    extracted = ExtractedJob(
        apply_url=page.final_url,
        domain=urlparse(page.final_url).netloc.lower(),
        title=title,
        company=company,
        location=location,
        remote_text=remote_text,
        body_text=body_text[:30000],
        apply_available=apply_available,
        posting_date=posting_date,
        is_open_hint=not closed,
        valid_through_expired=_valid_through_expired(structured.get("valid_through", "")),
    )
    provenance = {
        "title": title_source if title else "unknown",
        "company": company_source if company else "unknown",
        "location": location_source if location else "unknown",
        "remote_text": remote_source if remote_text else "unknown",
    }
    final = _apply_ats_hints(extracted, page, ats_hints)
    if final is not extracted:
        for field_name in ("title", "company", "location", "remote_text"):
            if getattr(final, field_name) != getattr(extracted, field_name):
                provenance[field_name] = "ats_hint"
    final._field_provenance = dict(provenance)
    return final, provenance


def parse_generic(page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob:
    """Extract a job with the generic HTML/JSON-LD parser and ATS hints."""
    extracted, _ = _parse_generic_with_provenance(page, ats_hints=ats_hints)
    return extracted


def parse_with_overrides(
    page: JobRawPage,
    *,
    ats_hints: dict[str, str] | None = None,
    title_selectors: Iterable[str] = (),
    company_selectors: Iterable[str] = (),
    location_selectors: Iterable[str] = (),
    remote_selectors: Iterable[str] = (),
    use_jsonld_description: bool = False,
) -> ExtractedJob:
    """Extract a job using vendor selectors, preserving field provenance."""
    base, base_sources = _parse_generic_with_provenance(
        page, ats_hints=ats_hints, use_jsonld_description=use_jsonld_description,
    )
    hints = _clean_ats_hints(ats_hints)
    soup = BeautifulSoup(page.html, "html.parser")

    title = _first_text(soup, title_selectors)
    company = _first_text(soup, company_selectors)
    location = _first_text(soup, location_selectors)
    remote_candidates = [
        (value, "selector") for value in _iter_selector_texts(soup, remote_selectors) if _looks_remote_workplace(value)
    ]

    updates: dict[str, str] = {}
    chosen_title = _pick_best_candidate(
        [(title, "selector"), (base.title, base_sources.get("title", "unknown"))],
        page_url=page.final_url,
        field="title",
    )
    chosen_company = _pick_best_candidate(
        [(company, "selector"), (base.company, base_sources.get("company", "unknown"))],
        page_url=page.final_url,
        field="company",
    )
    chosen_location = _pick_best_candidate(
        [(location, "selector"), (base.location, base_sources.get("location", "unknown"))],
        page_url=page.final_url,
        field="location",
    )
    if chosen_title and chosen_title != base.title:
        updates["title"] = chosen_title
    if chosen_company and chosen_company != base.company:
        updates["company"] = chosen_company
    if chosen_location and chosen_location != base.location:
        updates["location"] = chosen_location

    remote_candidate = _pick_best_candidate(
        [*remote_candidates, (base.remote_text, base_sources.get("remote_text", "unknown"))],
        page_url=page.final_url,
        field="remote",
    )
    remote_text = _dedupe_join([remote_candidate, base.remote_text], limit=4)
    if not remote_text and _looks_remote_workplace(chosen_location or base.location):
        remote_text = _clean_text(chosen_location or base.location)
    if remote_text:
        updates["remote_text"] = remote_text

    provenance = dict(base_sources)
    if title and _same_text(chosen_title, title):
        provenance["title"] = "selector"
    if company and _same_text(chosen_company, company):
        provenance["company"] = "selector"
    if location and _same_text(chosen_location, location):
        provenance["location"] = "selector"
    if any(_same_text(remote_candidate, value) for value, _source in remote_candidates):
        provenance["remote_text"] = "selector"

    if not updates:
        final = _apply_ats_hints(base, page, ats_hints)
    else:
        final = _apply_ats_hints(base.model_copy(update=updates), page, ats_hints)
    for field_name in ("title", "company", "location", "remote_text"):
        hint_key = "remote_hint" if field_name == "remote_text" else field_name
        if hint_key in hints and (
            (field_name == "remote_text" and hints[hint_key].lower() in getattr(final, field_name).lower())
            or (field_name != "remote_text" and _same_text(getattr(final, field_name), hints[hint_key]))
        ):
            provenance[field_name] = "ats_hint"
    final._field_provenance = provenance
    return final


def detect_ats_fingerprint(html: str) -> str | None:
    match = match_vendor_by_fingerprint(html)
    return match.name if match else None


def ats_canonical_domain(ats_vendor: str | None) -> str | None:
    return canonical_domain_for_vendor(ats_vendor)
