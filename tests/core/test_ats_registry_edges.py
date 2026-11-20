from __future__ import annotations

import pytest

from ultron.core.ats import registry
from ultron.core.ats.registry import (
    extract_seed_from_url,
    get_vendor_spec,
    infer_vendor_from_host,
    is_direct_supported_ats_host,
    supported_ats_probe_vendors,
)


@pytest.mark.parametrize(
    ("url", "vendor"),
    [
        ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://boards.greenhouse.io/embed/job_board?for=acme", "greenhouse"),
        ("https://jobs.lever.co/acme/1", "lever"),
        ("https://acme.ashbyhq.com/job/1", "ashby"),
        ("https://apply.workable.com/acme/j/1", "workable"),
        ("https://acme.personio.de/job/1", "personio"),
        ("https://acme.recruitee.com/o/1", "recruitee"),
        ("https://acme.teamtailor.com/jobs/1", "teamtailor"),
        ("https://careers.smartrecruiters.com/acme/company/1", "smartrecruiters"),
        ("https://acme.homerun.co/job/1", "homerun"),
        ("https://jobs.jobvite.com/acme/job/1", "jobvite"),
        ("https://acme.bamboohr.com/careers/1", "bamboohr"),
        ("https://acme.breezy.hr/p/1", "breezy"),
        ("https://acme.applytojob.com/apply/1", "jazzhr"),
        ("https://acme.factorialhr.com/job/1", "factorial"),
        ("https://acme.pinpointhq.com/postings/1", "pinpoint"),
        ("https://acme.comeet.com/jobs/1", "comeet"),
        ("https://acme.wd1.myworkdayjobs.com/en-US/External/job/1", "workday"),
        ("https://acme.icims.com/jobs/1", "icims"),
        ("https://acme.taleo.net/careersection/acme/job/1", "taleo"),
        ("https://acme.hirehive.com/staff-nurse-Ab12Cd", "hirehive"),
        ("https://api.occupop.com/api/jobs-frame/AcmeTok3n0123456789", "occupop"),
        ("https://www.candidatemanager.net/cm/p/pJobs.aspx?mid=ACME&sid=JOBS", "candidatemanager"),
    ],
)
def test_extract_seed_supports_each_registered_vendor(url, vendor):
    seed = extract_seed_from_url(url, "Acme Corp")

    assert seed is not None
    assert seed.ats == vendor
    assert seed.board_identifier
    assert seed.tags == ["api-discovered"]


def test_registry_rejects_marketing_pages_and_accepts_schemeless_urls():
    assert extract_seed_from_url("greenhouse.io/customers", "Acme") is None
    assert extract_seed_from_url("www.smartrecruiters.com/blog", "Acme") is None
    assert extract_seed_from_url("https://hire.lever.co/auth/login", "Acme") is None
    assert extract_seed_from_url("boards.greenhouse.io/acme/jobs/1", "Acme").ats == "greenhouse"
    assert is_direct_supported_ats_host("boards.greenhouse.io") is True
    assert is_direct_supported_ats_host("example.test") is False
    assert extract_seed_from_url("https://developers.pinpointhq.com/docs/channels", "") is None
    assert extract_seed_from_url("https://status.comeet.com", "") is None
    assert extract_seed_from_url("https://www.comeet.com", "Spark Hire") is None


@pytest.mark.parametrize("url", [
    "https://www.icims.com/products",
    "https://icims.com/",
    "https://www.taleo.net/solutions",
    "https://www.smartrecruiters.com/hr-software",
    "https://smartrecruiters.com/recruiting-software",
    "https://www.factorialhr.com/time-tracking",
    "https://factorialhr.com/es/pricing",
    "https://www.pinpointhq.com/features",
    "https://www.applytojob.com/features",
    "https://www.myworkdayjobs.com/External",
])
def test_vendor_marketing_hosts_are_never_boards(url):
    assert extract_seed_from_url(url, "Acme") is None


@pytest.mark.parametrize(("url", "board"), [
    ("https://careers-acme.icims.com/jobs/1/job", "careers-acme.icims.com"),
    ("https://acme.taleo.net/careersection/ext/jobsearch.ftl", "acme.taleo.net/careersection/ext"),
    ("https://jobs.smartrecruiters.com/Acme/123", "acme"),
    ("https://jobs.factorialhr.com/acme", "acme"),
    ("https://acme.factorialhr.com/", "acme"),
    ("https://acme.pinpointhq.com/postings/1", "acme"),
    ("https://acme.applytojob.com/apply/x", "acme"),
    ("https://www.comeet.com/jobs/acme/41.001", "acme"),
    ("https://www.smartrecruiters.com/Acme/743999123456789-senior-engineer", "acme"),
])
def test_tenant_hosts_next_to_marketing_hosts_still_give_boards(url, board):
    assert extract_seed_from_url(url, "Acme").board_identifier == board


def test_registry_lookup_and_probe_contracts():
    assert get_vendor_spec("GREENHOUSE").name == "greenhouse"
    assert get_vendor_spec("") is None
    assert infer_vendor_from_host("jobs.lever.co") == "lever"
    assert infer_vendor_from_host("example.test") is None
    names = {spec.name for spec in supported_ats_probe_vendors()}
    assert {"greenhouse", "lever", "ashby", "workable"}.issubset(names)


def test_registry_normalization_and_company_fallbacks():
    assert registry._normalize_board_identifier(" Acme! ") == "acme"
    assert registry._normalize_board_identifier("jobs") == ""
    assert registry._normalize_board_identifier("External", preserve_case=True) == "External"
    assert registry._host_prefix_labels("acme.lever.co", "lever.co") == ["acme"]
    assert registry._host_prefix_labels("example.test", "lever.co") is None
    assert registry._is_marketing_vendor_host("lever.co", "lever.co") is True
    assert registry._is_marketing_vendor_host("www.lever.co", "lever.co") is True
    assert registry._non_generic_host_labels("jobs.acme.example") == ["acme", "example"]
    assert registry._simple_company("", "acme-board") == "Acme Board"
    assert registry._simple_company("Acme", "acme-board") == "Acme"
    assert registry._simple_company("Other", "acme-board") == "Acme Board"
    assert registry._simple_company("Acme Board", "acme-board") == "Acme Board"


def test_registry_vendor_seed_extractors_cover_marketing_and_fallback_routes():
    assert registry._greenhouse_seed("https://boards.greenhouse.io/acme/jobs/1", "Acme") == "acme"
    assert registry._greenhouse_seed("https://boards.greenhouse.io/embed/job_board?for=acme", "") == "acme"
    assert registry._greenhouse_seed("https://boards.greenhouse.io/embed/job_board?for=123", "Acme") is None
    assert registry._greenhouse_seed("https://acme.greenhouse.io/?gh_jid=1", "Acme") == "acme"
    assert registry._greenhouse_seed("https://careers.acme.example/jobs?gh_jid=42", "Acme") == "acme"
    assert registry._greenhouse_seed("https://careers.acme.example/jobs?jobid=55555", "Acme") is None
    assert extract_seed_from_url("https://careers.acme.example/jobs?jobid=55555", "Acme") is None
    assert registry._greenhouse_seed("https://greenhouse.io/about", "Acme") is None

    assert registry._lever_seed("https://acme.lever.co/jobs", "") == "acme"
    assert registry._lever_seed("https://lever.co/customers", "") is None
    assert registry._ashby_seed("https://acme.ashbyhq.com/jobs", "") == "acme"
    assert registry._ashby_seed("https://ashbyhq.com/customers", "") is None
    assert registry._workable_seed("https://apply.workable.com/acme/j/1", "") == "acme"
    assert registry._workable_seed("https://apply.workable.com/acme/jobs/1", "") == "acme"
    assert registry._workable_seed("https://apply.workable.com/acme", "") == "acme"
    assert registry._subdomain_seed("https://www.personio.de/jobs", "", "personio.de") is None
    assert registry._path_seed("https://example.com/jobs/1", "", host_suffix="personio.de") is None

    assert registry._smartrecruiters_seed("https://careers.smartrecruiters.com/acme/company/acme", "") == "acme"
    assert registry._jobvite_seed("https://jobvite.com/customers", "") is None
    assert registry._factorial_seed("https://acme.factorialhr.com/jobs", "") == "acme"
    assert registry._jazzhr_seed("https://applytojob.com/apply/1", "Acme Corp") == "acme-corp"
    assert registry._pinpoint_seed("https://pinpointhq.com/postings/acme", "") == "acme"
    assert registry._comeet_seed("https://comeet.com/acme/jobs", "") == "acme"
    assert registry._comeet_seed("https://www.comeet.com/jobs/acme/41.001", "") == "acme"
    assert registry._comeet_seed("https://unknown.example/jobs", "Acme Corp") == "acme-corp"


def test_registry_workday_icims_taleo_and_parser_fallbacks():
    workday = registry._workday_seed("https://wd1.myworkdayjobs.com/External/job/1", "")
    assert workday == "wd1.myworkdayjobs.com/External"
    workday_site = registry._workday_seed("https://acme.myworkdaysite.com/en-US/External/details/1", "")
    assert workday_site == "acme.myworkdaysite.com/en-US/External"
    assert registry._workday_seed("https://example.test/jobs", "") is None
    assert registry._icims_seed("https://acme.icims.com/jobs/1", "") == "acme.icims.com"
    assert registry._icims_seed("https://example.test/jobs", "") is None
    assert (
        registry._taleo_seed("https://acme.taleo.net/careersection/acme/job/1", "")
        == "acme.taleo.net/careersection/acme"
    )
    assert registry._taleo_seed("https://acme.taleo.net/jobs/1", "") == "acme.taleo.net"

    assert registry.match_vendor_spec_by_domain("boards.greenhouse.io") is not None
    assert registry.match_vendor_spec_by_fingerprint("not an ATS page") is None
    page = registry.JobRawPage(
        url="https://unknown.example/job",
        final_url="unknown.example/job",
        status_code=200,
        html="<html><body>plain</body></html>",
    )
    parsed = registry.parse_ats_page(page)
    assert parsed.apply_url == "unknown.example/job"
