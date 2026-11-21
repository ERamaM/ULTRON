from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ultron.core.ats.vendors import canonical_domain_for_vendor, match_vendor_by_fingerprint
from ultron.core.extractors.jsonld import extract_structured_fields, valid_through_expired
from ultron.core.extractors.page_title import PAGE_TITLE_SOURCES, strip_page_title_suffix
from ultron.core.extractors.posting_date import posting_date_from_text
from ultron.core.extractors.text import (
    collapse_whitespace as clean_text,
)
from ultron.core.extractors.text import (
    dedupe_join,
    looks_remote_workplace,
    same_text,
)
from ultron.core.models import ExtractedJob, JobRawPage
from ultron.core.rules.geo_eu import EXTRACTOR_COMPENSATION_OVERRIDE_TERMS, EXTRACTOR_LOCATION_MARKERS
from ultron.core.rules.hard_filters_signals import has_closed_status_phrase

_LOCATION_MARKER_RE = re.compile(r"\b(?:location|based in|located in|" + "|".join(EXTRACTOR_LOCATION_MARKERS) + r")\b")
APPLY_HINT_TERMS = ("apply", "submit application", "apply now", "send application", "accepting applications")
LOCATION_LABEL_TERMS = ("location", "ubicación", "ubicacion", "where", "location:")
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
    "hirehive",
    "occupop",
    "candidatemanager",
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
        value = clean_text(value_raw)
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
            if not looks_remote_workplace(value):
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
    value = clean_text(node.get_text(" ", strip=True))
    if value:
        return value
    # <img> elements (and wrappers containing only an <img>) carry no text;
    # fall back to the image's alt attribute.
    if node.name == "img":
        return clean_text(str(node.get("alt") or ""))
    img = node.find("img") if hasattr(node, "find") else None
    if img is not None:
        return clean_text(str(img.get("alt") or ""))
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
            value = clean_text(node.get_text(" ", strip=True))
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
    return clean_text(str(node.get("content") or ""))



def extract_text_lines(text: str) -> list[str]:
    lines = [line.strip() for line in text.splitlines()]
    return [line for line in lines if line]


def _is_location_like_line(line: str) -> bool:
    clean = clean_text(line)
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
        looks_remote_workplace(clean)
        or bool(LOCATION_COMPENSATION_OVERRIDE_RE.search(low))
        or has_brief_comma_location
    )
    if _looks_compensation_line(low) and not has_strong_location_signal:
        return False
    if looks_remote_workplace(clean):
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
        clean = clean_text(line)
        if _is_location_like_line(clean):
            return clean
    for line in lines:
        clean = clean_text(line)
        if _is_location_like_line(clean):
            return clean
    return ""


def _extract_remote_lines(lines: list[str]) -> str:
    remote_lines = [ln for ln in lines if looks_remote_workplace(ln) and len(clean_text(ln)) <= 100]
    return dedupe_join(remote_lines, limit=4)


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _host_labels(url: str) -> list[str]:
    host = urlparse(url).netloc.lower()
    return [part for part in host.split(".") if part]


def _looks_domain_placeholder(value: str, page_url: str) -> bool:
    clean = clean_text(value)
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
    clean = clean_text(value)
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
    clean = clean_text(value)
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
        text = clean_text(str(value or ""))
        if text:
            cleaned[key] = text
    return cleaned


def _apply_ats_hints(
    extracted: ExtractedJob, page: JobRawPage, ats_hints: dict[str, str] | None, *, weak_company: bool = False,
) -> ExtractedJob:
    """Fill fields from the ATS hints. ``weak_company``: the company came only from ``og:site_name`` or the host
    name, which a portal may set to a generic word (an iCIMS hospital portal named "Nurses"), so the seed's
    company hint wins."""
    hints = _clean_ats_hints(ats_hints)
    if not hints:
        return extracted

    updates: dict[str, str] = {}
    title_hint = hints.get("title", "")
    if title_hint and (not extracted.title or _looks_generic_title(extracted.title, page.final_url)):
        updates["title"] = title_hint

    company_hint = hints.get("company", "")
    if company_hint and (
        weak_company or not extracted.company or _looks_generic_company(extracted.company, page.final_url)
    ):
        updates["company"] = company_hint

    location_hint = hints.get("location", "")
    # ATS hints are the highest-priority location source.  Location
    # heuristics are permissive enough to produce a non-empty but unrelated
    # line, so retaining any extracted value can hide a trusted board hint.
    if location_hint:
        updates["location"] = location_hint

    remote_hint = hints.get("remote_hint", "")
    if remote_hint and looks_remote_workplace(remote_hint):
        remote_text = dedupe_join([extracted.remote_text, remote_hint], limit=4)
        if remote_text and remote_text != extracted.remote_text:
            updates["remote_text"] = remote_text

    if not updates:
        return extracted
    return extracted.model_copy(update=updates)


# Company sources a seed's company hint beats: a site name or the host name, never JSON-LD or a vendor selector.
_WEAK_COMPANY_SOURCES = frozenset({"meta_og", "domain_fallback"})


def _parse_generic_with_provenance(
    page: JobRawPage, ats_hints: dict[str, str] | None = None, *, use_jsonld_description: bool = False,
    defer_weak_company: bool = False,
) -> tuple[ExtractedJob, dict[str, str]]:
    """Like parse_generic, but also returns the winning source per field.

    ``defer_weak_company``: keep a site-name or host-name company for the caller, which runs vendor selectors
    first and only then lets the company hint replace what is still weak (``parse_with_overrides``).

    The provenance dict maps field name ("title"/"company"/"location"/"remote_text")
    to a SOURCE_PRIORITY key (e.g. "jsonld", "h1", "line_heuristic", "ats_hint"),
    so callers re-scoring these values keep their true priority.
    """
    soup = BeautifulSoup(page.html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = extract_text_lines(text)
    structured = extract_structured_fields(soup, page.final_url)

    h1_text = ""
    h1 = soup.find("h1")
    if h1:
        h1_text = h1.get_text(" ", strip=True)
    hints = _clean_ats_hints(ats_hints)
    title_anchors = (structured.get("title", ""), h1_text, hints.get("title", ""))
    site_names = (
        structured.get("company", ""),
        _meta_content(soup, property_name="og:site_name"),
        hints.get("company", ""),
        _fallback_company_from_domain(page.final_url),
    )
    page_title_candidates = [
        (structured.get("title", ""), "jsonld"),
        (structured.get("title", ""), "jsonld"),
        (_meta_content(soup, property_name="og:title"), "meta_og"),
        (
            _meta_content(soup, name="twitter:title") or _meta_content(soup, property_name="twitter:title"),
            "meta_twitter",
        ),
        (h1_text, "h1"),
        ((soup.title.get_text(" ", strip=True) if soup.title else ""), "title_tag"),
    ]
    title_candidates = [
        (
            strip_page_title_suffix(value, anchors=title_anchors, site_names=site_names)
            if source in PAGE_TITLE_SOURCES
            else value,
            source,
        )
        for value, source in page_title_candidates
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
    location = clean_text(location)

    remote_candidates = [
        (structured.get("remote_text", ""), "jsonld"),
        (_extract_remote_lines(lines), "line_heuristic"),
    ]
    remote_text, remote_source = _pick_best_candidate_with_source(
        remote_candidates, page_url=page.final_url, field="remote"
    )
    if not remote_text and looks_remote_workplace(location):
        remote_text = location
        remote_source = location_source

    text_low = text.lower()
    apply_available = any(token in text_low for token in APPLY_HINT_TERMS)

    closed = has_closed_status_phrase(text)  # same phrase list as the hard filters

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
        valid_through_expired=valid_through_expired(structured.get("valid_through", "")),
    )
    provenance = {
        "title": title_source if title else "unknown",
        "company": company_source if company else "unknown",
        "location": location_source if location else "unknown",
        "remote_text": remote_source if remote_text else "unknown",
    }
    weak_company = company_source in _WEAK_COMPANY_SOURCES and not defer_weak_company
    final = _apply_ats_hints(extracted, page, ats_hints, weak_company=weak_company)
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
        page, ats_hints=ats_hints, use_jsonld_description=use_jsonld_description, defer_weak_company=True,
    )
    hints = _clean_ats_hints(ats_hints)
    soup = BeautifulSoup(page.html, "html.parser")

    title = _first_text(soup, title_selectors)
    company = _first_text(soup, company_selectors)
    location = _first_text(soup, location_selectors)
    remote_candidates = [
        (value, "selector") for value in _iter_selector_texts(soup, remote_selectors) if looks_remote_workplace(value)
    ]

    updates: dict[str, str] = {}
    chosen_title = _pick_best_candidate(
        [(title, "selector"), (base.title, base_sources.get("title", "unknown"))],
        page_url=page.final_url,
        field="title",
    )
    # With a company hint, a site-name or host-name company is only a placeholder the hint replaces below, so it
    # must not outrank a real company element found by the vendor selectors.
    base_company_source = base_sources.get("company", "unknown")
    if hints.get("company") and base_company_source in _WEAK_COMPANY_SOURCES:
        base_company_source = "unknown"
    chosen_company = _pick_best_candidate(
        [(company, "selector"), (base.company, base_company_source)],
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
    remote_text = dedupe_join([remote_candidate, base.remote_text], limit=4)
    if not remote_text and looks_remote_workplace(chosen_location or base.location):
        remote_text = clean_text(chosen_location or base.location)
    if remote_text:
        updates["remote_text"] = remote_text

    provenance = dict(base_sources)
    if title and same_text(chosen_title, title):
        provenance["title"] = "selector"
    if company and same_text(chosen_company, company):
        provenance["company"] = "selector"
    if location and same_text(chosen_location, location):
        provenance["location"] = "selector"
    if any(same_text(remote_candidate, value) for value, _source in remote_candidates):
        provenance["remote_text"] = "selector"

    # Only now, after the vendor selectors, may the company hint replace a site name or host name.
    weak_company = provenance.get("company") in _WEAK_COMPANY_SOURCES
    candidate = base.model_copy(update=updates) if updates else base
    final = _apply_ats_hints(candidate, page, ats_hints, weak_company=weak_company)
    for field_name in ("title", "company", "location", "remote_text"):
        hint_key = "remote_hint" if field_name == "remote_text" else field_name
        if hint_key in hints and (
            (field_name == "remote_text" and hints[hint_key].lower() in getattr(final, field_name).lower())
            or (field_name != "remote_text" and same_text(getattr(final, field_name), hints[hint_key]))
        ):
            provenance[field_name] = "ats_hint"
    final._field_provenance = provenance
    return final


def detect_ats_fingerprint(html: str) -> str | None:
    match = match_vendor_by_fingerprint(html)
    return match.name if match else None


def ats_canonical_domain(ats_vendor: str | None) -> str | None:
    return canonical_domain_for_vendor(ats_vendor)
