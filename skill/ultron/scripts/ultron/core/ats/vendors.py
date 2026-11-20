from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AtsVendorName = Literal[
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "personio",
    "recruitee",
    "teamtailor",
    "smartrecruiters",
    "homerun",
    "jobvite",
    "bamboohr",
    "breezy",
    "jazzhr",
    "factorial",
    "pinpoint",
    "comeet",
    "workday",
    "icims",
    "taleo",
    "hirehive",
]


@dataclass(frozen=True, slots=True)
class AtsVendorData:
    name: AtsVendorName
    label: str
    canonical_domain: str
    domains: tuple[str, ...]
    fingerprint_terms: tuple[str, ...]
    allow_domains: tuple[str, ...] = ()
    supports_dynamic_fallback: bool = False

    @property
    def all_domains(self) -> tuple[str, ...]:
        extra = tuple(domain for domain in self.allow_domains if domain not in self.domains)
        return (*self.domains, *extra)


VENDOR_DATA: tuple[AtsVendorData, ...] = (
    AtsVendorData(
        name="greenhouse",
        label="Greenhouse",
        canonical_domain="boards.greenhouse.io",
        domains=("greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io"),
        fingerprint_terms=("window.greenhouse", 'content="greenhouse"', "boards.greenhouse.io"),
    ),
    AtsVendorData(
        name="lever",
        label="Lever",
        canonical_domain="jobs.lever.co",
        domains=("lever.co", "jobs.lever.co", "jobs.eu.lever.co"),
        fingerprint_terms=("window.lever", 'content="lever"', "jobs.lever.co"),
    ),
    AtsVendorData(
        name="ashby",
        label="Ashby",
        canonical_domain="jobs.ashbyhq.com",
        domains=("ashbyhq.com", "jobs.ashbyhq.com"),
        fingerprint_terms=("ashbyhq.com", "window.ashby", "__ashby_state__"),
    ),
    AtsVendorData(
        name="workable",
        label="Workable",
        canonical_domain="apply.workable.com",
        domains=("workable.com", "apply.workable.com"),
        fingerprint_terms=("workable.com", "window.workable", "apply.workable.com"),
    ),
    AtsVendorData(
        name="personio",
        label="Personio",
        canonical_domain="jobs.personio.com",
        domains=("personio.de", "personio.com", "jobs.personio.de", "jobs.personio.com"),
        fingerprint_terms=("jobs.personio.", "personio.de/xml", "personio.com/xml", "workzag-jobs"),
    ),
    AtsVendorData(
        name="recruitee",
        label="Recruitee",
        canonical_domain="recruitee.com",
        domains=("recruitee.com",),
        fingerprint_terms=("recruitee.com", "recruitee-public", "recruitee-widget"),
    ),
    AtsVendorData(
        name="teamtailor",
        label="Teamtailor",
        canonical_domain="teamtailor.com",
        domains=("teamtailor.com",),
        fingerprint_terms=("teamtailor.com", "teamtailor-widget", "data-teamtailor"),
    ),
    AtsVendorData(
        name="smartrecruiters",
        label="SmartRecruiters",
        canonical_domain="smartrecruiters.com",
        domains=("smartrecruiters.com",),
        fingerprint_terms=("smartrecruiters", "smartrecruiters.com"),
    ),
    AtsVendorData(
        name="homerun",
        label="Homerun",
        canonical_domain="homerun.co",
        domains=("homerun.co",),
        fingerprint_terms=("homerun.co", "data-homerun", "homerun-job"),
    ),
    AtsVendorData(
        name="jobvite",
        label="Jobvite",
        canonical_domain="jobs.jobvite.com",
        domains=("jobvite.com", "jobs.jobvite.com", "jobs.eu.jobvite.com"),
        fingerprint_terms=("jobvite", "jobs.jobvite.com"),
    ),
    AtsVendorData(
        name="bamboohr",
        label="BambooHR",
        canonical_domain="bamboohr.com",
        domains=("bamboohr.com",),
        fingerprint_terms=("bamboohr", "bamboohr.com"),
    ),
    AtsVendorData(
        name="breezy",
        label="Breezy HR",
        canonical_domain="breezy.hr",
        domains=("breezy.hr",),
        fingerprint_terms=("breezy.hr", "bzopeningsportal", "breezy-fhr"),
    ),
    AtsVendorData(
        name="jazzhr",
        label="JazzHR",
        canonical_domain="applytojob.com",
        domains=("applytojob.com",),
        fingerprint_terms=("applytojob.com", "jazzhr"),
    ),
    AtsVendorData(
        name="factorial",
        label="Factorial",
        canonical_domain="jobs.factorialhr.com",
        domains=("factorialhr.com", "jobs.factorialhr.com"),
        fingerprint_terms=("factorialhr.com", "jobs.factorialhr.com", "factorialhr"),
    ),
    AtsVendorData(
        name="pinpoint",
        label="Pinpoint",
        canonical_domain="pinpointhq.com",
        domains=("pinpointhq.com",),
        fingerprint_terms=("pinpointhq.com", "pinpointhq", "pinpoint careers"),
    ),
    AtsVendorData(
        name="comeet",
        label="Comeet",
        canonical_domain="comeet.com",
        domains=("comeet.com", "comeet.co"),
        fingerprint_terms=("comeet.com", "comeet.co/jobs", "comeet-host"),
    ),
    AtsVendorData(
        name="workday",
        label="Workday",
        canonical_domain="myworkdayjobs.com",
        domains=("myworkdayjobs.com", "myworkdaysite.com"),
        fingerprint_terms=(
            "myworkdayjobs",
            "workday.com/wday",
            "wd1.myworkday",
            'data-automation-id="jobpostingheader"',
        ),
        supports_dynamic_fallback=True,
    ),
    AtsVendorData(
        name="icims",
        label="iCIMS",
        canonical_domain="icims.com",
        domains=("icims.com",),
        fingerprint_terms=("icims", "icims.com", "icims_jobpage"),
        supports_dynamic_fallback=True,
    ),
    AtsVendorData(
        name="taleo",
        label="Taleo",
        canonical_domain="taleo.net",
        domains=("taleo.net",),
        fingerprint_terms=("taleo", "taleo.net", "requisitiondescriptioninterface"),
        supports_dynamic_fallback=True,
    ),
    AtsVendorData(
        name="hirehive",
        label="HireHive",
        canonical_domain="hirehive.com",
        domains=("hirehive.com",),
        fingerprint_terms=("hirehive.com", "hh-job-listings", "hh-job-title"),
    ),
)


VENDOR_DATA_BY_NAME = {item.name: item for item in VENDOR_DATA}
SUPPORTED_ATS_NAMES = tuple(item.name for item in VENDOR_DATA)
SUPPORTED_ATS_DOMAINS = tuple(dict.fromkeys(domain for item in VENDOR_DATA for domain in item.all_domains))


def domain_matches(host: str, known_domain: str) -> bool:
    return host == known_domain or host.endswith(f".{known_domain}")


def match_vendor_by_domain(domain: str) -> AtsVendorData | None:
    host = str(domain or "").strip().lower()
    if not host:
        return None
    for item in VENDOR_DATA:
        if any(domain_matches(host, known_domain) for known_domain in item.domains):
            return item
    return None


def match_vendor_by_fingerprint(html: str) -> AtsVendorData | None:
    low = str(html or "").lower()
    if not low:
        return None
    for item in VENDOR_DATA:
        if any(term in low for term in item.fingerprint_terms):
            return item
    return None


def canonical_domain_for_vendor(vendor: str | None) -> str | None:
    if not vendor:
        return None
    item = VENDOR_DATA_BY_NAME.get(str(vendor).strip().lower())
    return item.canonical_domain if item else None


def greenhouse_job_url(board: str, job_id: str) -> str:
    """Canonical Greenhouse job URL — the single source for the boards.greenhouse.io scheme."""
    return f"https://boards.greenhouse.io/{board}/jobs/{job_id}"
