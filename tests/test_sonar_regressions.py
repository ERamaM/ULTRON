"""Regression tests for SonarCloud bug, ReDoS, extractor, and parity fixes."""

from __future__ import annotations

import json
import time

from ultron.core.ats.clients import workday
from ultron.core.discovery.prefilter import _has_non_eu_title_location
from ultron.core.extractors import smartrecruiters
from ultron.core.feeds import parsers
from ultron.core.models import JobRawPage
from ultron.core.rules.title_synonyms import normalize_title_separators


def test_normalize_title_separators_behavior_and_linear_time():
    assert normalize_title_separators("a - b") == "a b"
    assert normalize_title_separators("a -b") == "a b"
    assert normalize_title_separators("a- b") == "a b"
    assert normalize_title_separators("full-stack") == "full-stack"
    assert normalize_title_separators("a - - b") == "a  b"

    t0 = time.perf_counter()
    long_ws = (" " * 10_000) + "a"
    assert normalize_title_separators(long_ws) == long_ws
    assert (time.perf_counter() - t0) < 0.2


def test_has_non_eu_title_location_behavior_and_linear_time():
    assert _has_non_eu_title_location("Senior Engineer, USA") is True
    assert _has_non_eu_title_location("Backend Engineer - UK") is True
    assert _has_non_eu_title_location("ML Engineer (India)") is True
    assert _has_non_eu_title_location("Full-Stack Engineer-UK") is False

    t0 = time.perf_counter()
    assert _has_non_eu_title_location(", " + (" " * 10_000) + "foo") is False
    assert (time.perf_counter() - t0) < 0.2


def test_extract_company_entries_remote_es_parens_and_linear_time():
    md = "* Acme (Madrid (ES)) (Remote) [Open positions](https://example.com)\n"
    assert parsers.extract_company_entries_from_remote_es_markdown(md) == [("Acme", "https://example.com")]

    t0 = time.perf_counter()
    assert parsers.extract_company_entries_from_remote_es_markdown("* " + (" " * 10_000)) == []
    assert (time.perf_counter() - t0) < 0.2


def test_workday_extract_from_next_data_linear_on_pathological_scripts():
    payload = {"props": {"pageProps": {"jobs": [{"title": "Staff ML Engineer", "externalPath": "/job/9", "locationsText": "Remote"}]}}}
    # Include Unicode 'İ' (which expands in str.lower()) and prose mentioning __NEXT_DATA__ before the script tag.
    html = (
        f"<html><p>İ __NEXT_DATA__ note</p>"
        f"<script id='other'>{{}}</script>"
        f'<SCRIPT id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script></html>'
    )
    postings = workday._extract_from_next_data(html, "wd.example", "Careers")
    assert len(postings) == 1
    assert postings[0].title == "Staff ML Engineer"

    for bad_html in (
        "<script " * 2_000,
        ("<script " * 2_000) + 'id="__NEXT_DATA__"',
    ):
        t0 = time.perf_counter()
        assert workday._extract_from_next_data(bad_html, "wd.example") == []
        assert (time.perf_counter() - t0) < 0.5


def test_smartrecruiters_parse_meta_date_posted_and_missing_meta():
    page_with_date = JobRawPage(
        url="https://jobs.smartrecruiters.com/Acme/1",
        final_url="https://jobs.smartrecruiters.com/Acme/1",
        status_code=200,
        html='<html><head><meta itemprop="datePosted" content="2026-11-15"></head><body><h1>Backend Engineer</h1></body></html>',
    )
    job = smartrecruiters.parse(page_with_date, ats_hints={"title": "Backend Engineer"})
    assert job.posting_date == "2026-11-15"

    page_without_date = JobRawPage(
        url="https://jobs.smartrecruiters.com/Acme/2",
        final_url="https://jobs.smartrecruiters.com/Acme/2",
        status_code=200,
        html="<html><body><h1>Backend Engineer</h1></body></html>",
    )
    job_no_date = smartrecruiters.parse(page_without_date, ats_hints={"title": "Backend Engineer"})
    assert job_no_date.posting_date is None
