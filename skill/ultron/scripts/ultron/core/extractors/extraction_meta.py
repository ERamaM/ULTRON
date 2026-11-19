from __future__ import annotations

from bs4 import BeautifulSoup

from ultron.core.extractors.ats_extractors import base as ats_base
from ultron.core.models import ExtractedJob, ExtractionMeta, JobRawPage, SignalConfidence, SignalSource


def _same(a: str, b: str) -> bool:
    return ats_base._clean_text(a).lower() == ats_base._clean_text(b).lower()


def _remote_conflict_terms(text: str) -> bool:
    low = (text or "").lower()
    return any(token in low for token in ats_base.REMOTE_CONFLICT_TERMS)


def _field_meta(source: SignalSource) -> tuple[SignalSource, SignalConfidence]:
    confidence_by_source: dict[SignalSource, SignalConfidence] = {
        "ats_hint": "high",
        "jsonld": "high",
        "meta_og": "medium",
        "meta_twitter": "medium",
        "selector": "medium",
        "h1": "medium",
        "title_tag": "low",
        "line_heuristic": "low",
        "domain_fallback": "low",
        "unknown": "low",
    }
    return source, confidence_by_source.get(source, "low")


def build_extraction_meta(
    page: JobRawPage,
    extracted: ExtractedJob,
    *,
    ats_hints: dict[str, str] | None = None,
) -> ExtractionMeta:
    hints = ats_base._clean_ats_hints(ats_hints)
    soup = BeautifulSoup(page.html or "", "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = ats_base.extract_text_lines(text)
    structured = ats_base._extract_structured_fields(soup)

    og_title = ats_base._meta_content(soup, property_name="og:title")
    twitter_title = ats_base._meta_content(soup, name="twitter:title")
    if not twitter_title:
        twitter_title = ats_base._meta_content(soup, property_name="twitter:title")
    h1_node = soup.find("h1")
    h1_text = ats_base._clean_text(h1_node.get_text(" ", strip=True) if h1_node else "")
    title_tag = ats_base._clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")

    og_site = ats_base._meta_content(soup, property_name="og:site_name")
    line_location = ats_base._extract_location_from_lines(lines)
    line_remote = ats_base._extract_remote_lines(lines)
    domain_company = ats_base._fallback_company_from_domain(page.final_url)

    conflicts: list[str] = []
    embedded_provenance = dict(getattr(extracted, "_field_provenance", {}) or {})
    if _remote_conflict_terms(extracted.remote_text) and "remote" in (extracted.remote_text or "").lower():
        conflicts.append("remote_conflict_terms")
    if hints.get("company") and ats_base._looks_generic_company(domain_company, page.final_url):
        conflicts.append("company_placeholder_replaced")

    title_source: SignalSource = embedded_provenance.get("title", "unknown")  # type: ignore[assignment]
    if title_source != "unknown":
        pass
    elif hints.get("title") and _same(extracted.title, hints["title"]):
        title_source = "ats_hint"
    elif structured.get("title") and _same(extracted.title, structured["title"]):
        title_source = "jsonld"
    elif og_title and _same(extracted.title, og_title):
        title_source = "meta_og"
    elif twitter_title and _same(extracted.title, twitter_title):
        title_source = "meta_twitter"
    elif h1_text and _same(extracted.title, h1_text):
        title_source = "h1"
    elif title_tag and _same(extracted.title, title_tag):
        title_source = "title_tag"

    company_source: SignalSource = embedded_provenance.get("company", "unknown")  # type: ignore[assignment]
    if company_source != "unknown":
        pass
    elif hints.get("company") and _same(extracted.company, hints["company"]):
        company_source = "ats_hint"
    elif structured.get("company") and _same(extracted.company, structured["company"]):
        company_source = "jsonld"
    elif og_site and _same(extracted.company, og_site):
        company_source = "meta_og"
    elif domain_company and _same(extracted.company, domain_company):
        company_source = "domain_fallback"

    location_source: SignalSource = embedded_provenance.get("location", "unknown")  # type: ignore[assignment]
    if location_source != "unknown":
        pass
    elif hints.get("location") and _same(extracted.location, hints["location"]):
        location_source = "ats_hint"
    elif structured.get("location") and _same(extracted.location, structured["location"]):
        location_source = "jsonld"
    elif line_location and _same(extracted.location, line_location):
        location_source = "line_heuristic"

    remote_source: SignalSource = embedded_provenance.get("remote_text", "unknown")  # type: ignore[assignment]
    if remote_source != "unknown":
        pass
    elif hints.get("remote_hint") and hints["remote_hint"].lower() in (extracted.remote_text or "").lower():
        remote_source = "ats_hint"
    elif structured.get("remote_text") and structured["remote_text"].lower() in (extracted.remote_text or "").lower():
        remote_source = "jsonld"
    elif (line_remote and line_remote.lower() in (extracted.remote_text or "").lower()) or (
        extracted.remote_text and extracted.location and _same(extracted.remote_text, extracted.location)
    ):
        remote_source = "line_heuristic"

    title_source, title_conf = _field_meta(title_source)
    company_source, company_conf = _field_meta(company_source)
    location_source, location_conf = _field_meta(location_source)
    remote_source, remote_conf = _field_meta(remote_source)

    return ExtractionMeta(
        title_source=title_source,
        title_confidence=title_conf,
        company_source=company_source,
        company_confidence=company_conf,
        location_source=location_source,
        location_confidence=location_conf,
        remote_source=remote_source,
        remote_confidence=remote_conf,
        field_conflicts=conflicts[:6],
    )
