"""Declarative CSS-selector table for the config-shaped ATS extractors.

Most vendors' page parsers differ only in four selector tuples (title, company,
location, remote); they all delegate to :func:`parse_with_overrides`. Rather than
one near-identical module per vendor, the selectors live here as data and
:func:`make_selector_parser` builds the parser callable the registry wires in.

Vendors whose extraction needs real logic (e.g. Ashby's scoped-search fallback)
keep their own module and are not listed here.
"""

from __future__ import annotations

from ultron.core.extractors.ats_extractors.base import parse_with_overrides
from ultron.core.extractors.ats_extractors.parser_contract import ParserFn
from ultron.core.models import ExtractedJob, JobRawPage

SelectorSet = dict[str, tuple[str, ...]]

VENDOR_SELECTORS: dict[str, SelectorSet] = {
    "greenhouse": {
        "title": ("h1.app-title", "h1[data-qa='job-title']", "header h1"),
        "company": ("[data-qa='company-name']", ".company-name"),
        "location": ("[data-qa='job-location']", "[data-qa='location']", ".location", ".job__location"),
        "remote": (
            "[data-qa='workplaceType']",
            "[data-qa='job-location']",
            "[data-qa='location']",
            ".location",
            ".job__location",
        ),
    },
    "lever": {
        "title": ("h2.posting-headline", "h1"),
        "company": ("a.main-header-logo", ".posting-company h3"),
        "location": (".posting-categories .sort-by-location", ".posting-categories .location", ".location"),
        "remote": (
            ".posting-categories .workplaceTypes",
            ".posting-categories .sort-by-location",
            ".posting-categories .location",
            ".location",
        ),
    },
    "workable": {
        "title": ("h1[data-ui='job-title']", "h1"),
        "company": ("[data-ui='company-name']", ".company-name"),
        "location": ("[data-ui='job-location']", ".job-location"),
        "remote": ("[data-ui='job-workplace']", "[data-ui='job-location']", ".job-location"),
    },
    "personio": {
        "title": ("h1[data-test='job-title']", "h1.position-title", ".job__title h1", "h1"),
        "company": ("[data-test='company-name']", ".job__company", ".company-name"),
        "location": ("[data-test='job-location']", "[data-test='location']", ".job__location", ".position-location"),
        "remote": (
            "[data-test='workplace']",
            "[data-test='job-location']",
            "[data-test='location']",
            ".job__location",
        ),
    },
    "recruitee": {
        "title": ("h1.offer-title", "#offer-details h1", "[class*='offer-title']", "header h1", "h1"),
        "company": (".job-company-name", ".company-name"),
        "location": (".offer-location", "[class*='offer-location']", "#offer-details [class*='location']", ".location"),
        "remote": (".offer-location", "[class*='offer-location']", "#offer-details [class*='location']", ".location"),
    },
    "teamtailor": {
        "title": ("[data-testid='job-title']", ".job-page__title", ".job-title", "h1"),
        "company": ("[data-testid='company-name']", ".company-name", ".job-page__company"),
        "location": ("[data-testid='job-location']", ".job-page__location", ".job-detail-location"),
        "remote": ("[data-testid='job-workplace']", "[data-testid='job-location']", ".job-page__location"),
    },
    "smartrecruiters": {
        "title": ("[data-testid='job-title']", ".job-title", ".job-ad-header h1", "h1"),
        "company": ("[data-testid='company-name']", ".company-name", ".job-ad-company"),
        "location": ("[data-testid='job-location']", ".job-location", ".job-ad-location"),
        "remote": ("[data-testid='job-workplace']", "[data-testid='job-location']", ".job-location"),
    },
    "homerun": {
        "title": ("[data-test='vacancy-title']", ".vacancy__title", ".job-title", "h1"),
        "company": ("[data-test='company-name']", ".vacancy__company", ".company-name"),
        "location": ("[data-test='vacancy-location']", ".vacancy__location", ".job-location"),
        "remote": ("[data-test='vacancy-workplace']", "[data-test='vacancy-location']", ".vacancy__location"),
    },
    "bamboohr": {
        "title": (".BambooHR-ATS-JobDetail-Header h1", ".BambooHR-ATS-Jobs-Item-Title", "h1"),
        "company": (".BambooHR-ATS-CompanyName", ".company-name"),
        "location": (".BambooHR-ATS-JobLocation", ".BambooHR-ATS-Location", ".job-location"),
        "remote": (".BambooHR-ATS-RemoteType", ".BambooHR-ATS-JobLocation", ".job-location"),
    },
    "breezy": {
        "title": ("h1.position-title", ".position__title", "h1"),
        "company": (".company-name", ".position__company"),
        "location": (".position-location", ".position__location", ".job-location"),
        "remote": (".position__workplace", ".position-location", ".job-location"),
    },
    "jazzhr": {
        "title": ("h1.job-title", ".job-detail-title h1", "h1"),
        "company": (".company-name", ".job-company"),
        "location": (".job-location", ".job-meta .location", "[data-testid='job-location']"),
        "remote": (".job-workplace", ".job-location", ".job-meta .location"),
    },
    "factorial": {
        "title": ("[data-testid='job-title']", ".job-title", "h1"),
        "company": ("[data-testid='company-name']", ".company-name"),
        "location": ("[data-testid='job-location']", ".job-location", ".position-location"),
        "remote": ("[data-testid='job-workplace']", "[data-testid='job-location']", ".job-location"),
    },
    "pinpoint": {
        "title": ("[data-testid='job-title']", ".posting-title", ".job-title", "h1"),
        "company": ("[data-testid='company-name']", ".posting-company", ".company-name"),
        "location": ("[data-testid='job-location']", ".posting-location", ".job-location"),
        "remote": ("[data-testid='job-workplace']", "[data-testid='job-location']", ".posting-location"),
    },
    "comeet": {
        "title": ("h1.position-name", ".job-title", "h1"),
        "company": (".company-name", ".position-company"),
        "location": (".position-location", ".job-location", "[data-testid='job-location']"),
        "remote": (".position-workplace", ".position-location", ".job-location"),
    },
    "jobvite": {
        "title": ("h1.jv-job-title", ".jv-career-title h1", ".job-title", "h1"),
        "company": (".jv-header-logo img[alt]", ".company-name"),
        "location": (".jv-job-detail-meta", ".job-location", "[data-testid='job-location']"),
        "remote": (".jv-job-detail-meta", "[data-testid='job-location']", ".job-location"),
    },
    "workday": {
        "title": ("[data-automation-id='jobPostingHeader']", "h2[data-automation-id='jobPostingHeader']", "h1"),
        "company": ("[data-automation-id='company']", ".company-name"),
        "location": ("[data-automation-id='locations']", "[data-automation-id='location']", ".job-location"),
        "remote": ("[data-automation-id='remoteType']", "[data-automation-id='locations']", ".job-location"),
    },
    "icims": {
        "title": ("h1.iCIMS_JobPageTitle", ".iCIMS_Header h1", "h1"),
        "company": (".iCIMS_CompanyName", ".company-name"),
        "location": (".iCIMS_JobHeader .iCIMS_JobLocation", "div[data-id='job-location']", ".job-location"),
        "remote": (".iCIMS_JobHeader .iCIMS_JobLocation", "div[data-id='job-location']", ".job-location"),
    },
    # Taleo element IDs embed tenant-specific numbers (e.g. ID1665), so match by
    # attribute-prefix/suffix selectors that work across any tenant.
    "taleo": {
        "title": ("[id^='requisitionDescriptionInterface'][id$='_header']", ".titlepage h1", "h1"),
        "company": (".company-name", ".titlepage .company"),
        "location": (".titlepage .location", ".job-location"),
        "remote": (".titlepage .location", ".job-location"),
    },
}


def make_selector_parser(vendor: str) -> ParserFn:
    """Build a ``parse(page, ats_hints)`` callable from the vendor's selector set."""
    selectors = VENDOR_SELECTORS[vendor]

    def parse(page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob:
        return parse_with_overrides(
            page,
            ats_hints=ats_hints,
            title_selectors=selectors["title"],
            company_selectors=selectors["company"],
            location_selectors=selectors["location"],
            remote_selectors=selectors["remote"],
        )

    return parse
