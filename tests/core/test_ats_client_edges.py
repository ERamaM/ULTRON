"""ATS client edge cases ported from the MARK II project (fallback hosts, HTML fallbacks, malformed rows)."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest

from ultron.core.ats.clients import (
    AtsClientError,
    bamboohr,
    comeet,
    factorial,
    greenhouse,
    icims,
    jazzhr,
    jobvite,
    lever,
    personio,
    pinpoint,
    smartrecruiters,
    taleo,
    workable,
    workday,
)


class FC:
    def __init__(self, json_fn=None, text_fn=None):
        self.json_fn = json_fn or boom("no json")
        self.text_fn = text_fn or boom("no text")
        self.calls = []

    def request_json(self, url, method="GET", json_body=None, timeout=None):
        self.calls.append(url)
        return self.json_fn(url)

    def request_text(self, url, timeout=None):
        self.calls.append(url)
        return self.text_fn(url)


def boom(msg):
    def f(*a, **k):
        raise RuntimeError(msg)
    return f


def test_greenhouse_canonicalizes_ids_and_skips_malformed():
    c = FC(lambda u: {"jobs": [
        {"id": 123, "title": "ML", "location": {"name": "Remote"}, "updated_at": "2026-01-01"},
        {"absolute_url": "https://boards.greenhouse.io/acme/jobs/old?gh_jid=456", "title": "Data"},
        {"id": "", "absolute_url": "", "title": "Missing URL"}, {"id": "789", "title": ""}, "malformed"]})
    jobs = greenhouse.fetch_greenhouse_jobs("acme", client=c)
    assert [j.apply_url for j in jobs] == [
        "https://boards.greenhouse.io/acme/jobs/123", "https://boards.greenhouse.io/acme/jobs/456"]


def test_bamboohr_api_and_html_fallback():
    c = FC(lambda u: {"result": [{"jobOpeningName": "Engineer", "id": "7", "location": "Remote"}]})
    assert bamboohr.fetch_bamboohr_jobs("acme", client=c)[0].apply_url.endswith("/careers/7")
    c = FC(boom("api down"), lambda u: '<a class="BambooHR-ATS-Jobs-Item-Title" href="/7">Bamboo fallback</a>')
    assert bamboohr.fetch_bamboohr_jobs("acme", client=c)[0].title == "Bamboo fallback"


def test_factorial_empty_api_falls_back_to_html():
    c = FC(lambda u: {"jobs": []}, lambda u: '<a class="job-card__title" href="/job/1">Factorial role</a>')
    assert factorial.fetch_factorial_jobs("acme", client=c)[0].apply_url.endswith("/job/1")


def test_factorial_failed_api_falls_back_to_html():
    c = FC(boom("api down"), lambda u: '<a class="job-card__title" href="/7">Factorial fallback</a>')
    assert factorial.fetch_factorial_jobs("acme", client=c)[0].title == "Factorial fallback"


def test_pinpoint_empty_api_falls_back_to_html():
    c = FC(lambda u: {"jobs": []}, lambda u: '<a class="posting-card__title" href="/p/1">Pinpoint role</a>')
    assert pinpoint.fetch_pinpoint_jobs("acme", client=c)[0].apply_url.endswith("/p/1")


def test_pinpoint_failed_api_falls_back_to_html():
    c = FC(boom("api"), lambda u: '<a class="posting-card__title" href="/jobs/1">Engineer</a>')
    jobs = pinpoint.fetch_pinpoint_jobs("acme", client=c)
    assert jobs[0].apply_url == "https://acme.pinpointhq.com/jobs/1"


def test_pinpoint_api_rows():
    c = FC(lambda u: {"jobs": [{"title": "API", "url": "https://p/1"}]})
    assert pinpoint.fetch_pinpoint_jobs("acme", client=c)[0].title == "API"


def test_personio_xml():
    xml = ('<?xml version="1.0"?><workzag-jobs><position><id>9</id><name>Personio role</name>'
           '<office>Madrid</office><date>2026-01-01</date></position></workzag-jobs>')
    p = personio.fetch_personio_jobs("acme", client=FC(text_fn=lambda u: xml))[0]
    assert p.apply_url.endswith("/job/9") and p.location == "Madrid"


def test_jazzhr_html():
    html = '<div class="job-listing"><a href="/1"><span class="job-title">Jazz role</span></a></div>'
    assert jazzhr.fetch_jazzhr_jobs("acme", client=FC(text_fn=lambda u: html))[0].apply_url.endswith("/1")


def test_jobvite_location():
    html = '<div class="jv-job-list"><a href="https://job/1" data-title="Jobvite role" data-location="Remote"></a></div>'
    assert jobvite.fetch_jobvite_jobs("acme", client=FC(text_fn=lambda u: html))[0].location == "Remote"


def test_jobvite_eu_host_retry_and_exhaustion():
    html2 = 'jv-job-list data-title="Engineer" href="/jobs/1" data-location="EU"'

    def t(u):
        if urlsplit(u).hostname == "jobs.jobvite.com":
            raise RuntimeError("primary down")
        return html2
    c = FC(text_fn=t)
    jobs = jobvite.fetch_jobvite_jobs("acme", client=c)
    assert len(jobs) == 1 and jobs[0].title == "Engineer"
    assert urlsplit(c.calls[-1]).hostname == "jobs.eu.jobvite.com"
    assert jobs[0].apply_url == "https://jobs.eu.jobvite.com/jobs/1"
    with pytest.raises(RuntimeError, match="down"):
        jobvite.fetch_jobvite_jobs("acme", client=FC(text_fn=boom("down")))
    assert jobvite._extract_text_between('data-title="unterminated', 'data-title="') == ""
    empty_board = '<div class="jv-page-body"><p class="jv-no-jobs">No open jobs</p></div>'
    assert jobvite.fetch_jobvite_jobs("empty", client=FC(text_fn=lambda u: empty_board)) == []


def test_jobvite_reads_the_job_table_of_current_career_sites():
    # Current career sites list jobs in a table: the title is the anchor text, the location a sibling cell.
    html = """
    <style>.jv-job-list .jv-job-list-name { padding: 0 }</style>
    <table class="jv-job-list"><tbody>
    <tr><td class="jv-job-list-name"><h3 class="h2 jv-underline"><a href="/acme/job/oAbc1">Data Analyst</a></h3></td>
        <td class="jv-job-list-location">Analytics |
            São Paulo,
            Brazil</td></tr>
    <tr><td class="jv-job-list-name"><h3><a href="https://jobs.jobvite.com/acme/job/oAbc2">Support Engineer</a></h3></td>
        <td class="jv-job-list-location">Remote</td></tr>
    </tbody></table>"""
    jobs = jobvite.fetch_jobvite_jobs("acme", client=FC(text_fn=lambda u: html))
    assert [(j.title, j.apply_url, j.location) for j in jobs] == [
        ("Data Analyst", "https://jobs.jobvite.com/acme/job/oAbc1", "Analytics | São Paulo, Brazil"),
        ("Support Engineer", "https://jobs.jobvite.com/acme/job/oAbc2", "Remote"),
    ]


def test_jobvite_unknown_tenant_is_not_an_empty_board():
    # An unknown tenant redirects to the vendor's support site: no Jobvite career page markup on either host.
    # It used to come back as a board with zero jobs, so `seeds verify` verified any made-up slug.
    support = "<html><title>Job Seeker FAQs and Support</title><body>Help center</body></html>"
    with pytest.raises(AtsClientError, match="board_not_found"):
        jobvite.fetch_jobvite_jobs("made-up", client=FC(text_fn=lambda u: support))


def test_icims_html():
    html = ('<div><a href="/job/1"><span class="iCIMS_JobsTable_title">ICIMS role</span>'
            '<span class="iCIMS_JobsTable_location">Madrid</span></a></div>')
    c = FC(text_fn=lambda u: html if "pr=0" in u else "")
    assert icims.fetch_icims_jobs("acme.example", client=c)[0].location == "Madrid"


def test_taleo_html():
    t = '<a class="jobTitle-link" href="/career/1">Taleo role</a>'
    assert taleo.fetch_taleo_jobs("acme.taleo.net", client=FC(text_fn=lambda u: t))[0].apply_url.endswith("/career/1")


@pytest.mark.parametrize("locator", [
    "acme.taleo.net/careersection/ext",
    "acme.taleo.net/careersection/ext/",
    "acme.taleo.net/careersection/ext/joblist.ftl?lang=en",
])
def test_taleo_resolves_relative_jobs_in_the_career_section(locator):
    html = '<a class="jobTitle-link" href="jobdetail.ftl?job=42">Taleo role</a>'
    result = taleo.fetch_taleo_jobs(locator, client=FC(text_fn=lambda _url: html))
    assert result[0].apply_url == "https://acme.taleo.net/careersection/ext/jobdetail.ftl?job=42"


def test_comeet_html():
    cm = '<a href="/jobs/acme/company/position/role">Comeet role</a>'
    assert comeet.fetch_comeet_jobs("acme", client=FC(text_fn=lambda u: cm))[0].title == "Comeet role"


def test_workday_realistic_rows():
    payloads = iter([{"jobPostings": [{"title": "Workday role", "externalPath": "/job/1", "locationsText": "Remote"}]},
                     {"jobPostings": []}])
    ps = workday.fetch_workday_jobs("wd1.myworkdayjobs.com/External", client=FC(lambda u: next(payloads)))
    assert ps[0].apply_url.endswith("/job/1")


def test_workday_next_data_helper():
    payload = {"props": {"pageProps": {"jobs": [{"title": "Nested", "externalPath": "/job/1", "locationsText": "EU"}]}}}
    html = f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
    assert workday._extract_from_next_data(html, "wd.example")[0].apply_url.endswith("/job/1")
    assert workday._extract_from_next_data("<html></html>", "wd.example") == []
    assert workday._extract_from_next_data('<script id="__NEXT_DATA__">bad</script>', "wd.example") == []


def test_lever_retries_eu_and_filters_invalid():
    n = {"c": 0}

    def j(u):
        n["c"] += 1
        if n["c"] == 1:
            raise RuntimeError("primary down")
        return ["malformed", {"text": "", "hostedUrl": ""}, {"text": "Engineer", "hostedUrl": "https://jobs/1"}]
    jobs = lever.fetch_lever_jobs("acme", client=FC(j))
    assert len(jobs) == 1 and n["c"] == 2
    with pytest.raises(RuntimeError, match="down"):
        lever.fetch_lever_jobs("acme", client=FC(boom("down")))


def test_smartrecruiters_fallback_urls():
    page = [{"name": "Engineer", "id": "1", "location": {"city": "Madrid"}},
            {"title": "Data", "applyUrl": "https://apply/2", "department": "ML"},
            {"name": "Ref", "ref": "https://api/3"}, {"name": "Invalid"}]
    r = iter([{"content": page}, {"content": []}])
    jobs = smartrecruiters.fetch_smartrecruiters_jobs("acme", client=FC(lambda u: next(r)))
    assert [j.apply_url for j in jobs] == [
        "https://careers.smartrecruiters.com/acme/1", "https://apply/2", "https://api/3"]


def test_smartrecruiters_full_page_with_malformed_row_paginates():
    full = [{"name": f"Role {i}", "id": str(i)} for i in range(100)]
    full.insert(0, "malformed")
    r = iter([{"content": full}, {"content": []}])
    assert len(smartrecruiters.fetch_smartrecruiters_jobs("acme", client=FC(lambda u: next(r)))) == 100


def test_workable_ignores_malformed():
    c = FC(lambda u: {"jobs": ["bad", {"title": "", "url": ""}, {"title": "Engineer", "url": "https://job"}]})
    jobs = workable.fetch_workable_jobs("acme", client=c)
    assert len(jobs) == 1 and jobs[0].title == "Engineer"
    assert workable.fetch_workable_jobs("acme", client=FC(lambda u: [])) == []
