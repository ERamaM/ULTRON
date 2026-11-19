"""Port of MARK III test_ats_client_parsing.py for the 16 remaining vendors."""

from __future__ import annotations

from typing import get_args

import pytest

from ultron.core.ats.clients import FETCHERS, AtsClientError, fetch_vendor_jobs
from ultron.core.ats.clients.base import extract_between, locator_has_host, locator_url, parse_card_listings
from ultron.core.models import AtsBoardSeed
from ultron.net import RequestBudgetExceeded


class FakeClient:
    def __init__(self, json_value=None, text_value=""):
        self.json_value = json_value
        self.text_value = text_value
        self.urls: list[str] = []

    def request_json(self, url, method="GET", json_body=None, timeout=None):
        self.urls.append(url)
        return self.json_value

    def request_text(self, url, timeout=None):
        self.urls.append(url)
        if isinstance(self.text_value, Exception):
            raise self.text_value
        return self.text_value


# FR-1: host-style boards must live on the vendor's own domain (a custom domain is "invalid_board").
HOST_BOARDS = {"icims": "careers-acme.icims.com", "taleo": "acme.taleo.net"}


def _fields(postings):
    return [(p.title, p.apply_url, p.location, p.posted_at) for p in postings]


def fetch(vendor, board, client):
    return fetch_vendor_jobs(vendor, board, client=client, timeout=5)


def test_all_19_vendors_registered():
    assert set(FETCHERS) == set(get_args(AtsBoardSeed.model_fields["ats"].annotation))
    assert len(FETCHERS) == 19


def test_recruitee_parses_offers():
    payload = {
        "offers": [
            {
                "title": "ML Engineer",
                "careers_url": "https://acme.recruitee.com/o/ml",
                "location": "Remote",
                "published_at": "2026-01-01",
            },
            {"title": "No URL"},
            {
                "title": "By url key",
                "url": "https://acme.recruitee.com/o/x",
                "location_name": "Madrid",
                "created_at": "2026-02-02",
            },
        ]
    }
    assert _fields(fetch("recruitee", "acme", FakeClient(payload))) == [
        ("ML Engineer", "https://acme.recruitee.com/o/ml", "Remote", "2026-01-01"),
        ("By url key", "https://acme.recruitee.com/o/x", "Madrid", "2026-02-02"),
    ]


def test_teamtailor_parses_attributes_and_path():
    payload = {
        "data": [
            {
                "attributes": {
                    "title": "Data Scientist",
                    "path": "/jobs/1",
                    "location": "Berlin",
                    "created_at": "2026-03-03",
                }
            },
            {
                "attributes": {
                    "name": "By name",
                    "careers_url": "https://x.teamtailor.com/jobs/2",
                    "published_at": "2026-04-04",
                }
            },
        ]
    }
    assert _fields(fetch("teamtailor", "x", FakeClient(payload))) == [
        ("Data Scientist", "https://x.teamtailor.com/jobs/1", "Berlin", "2026-03-03"),
        ("By name", "https://x.teamtailor.com/jobs/2", "", "2026-04-04"),
    ]


def test_homerun_builds_url_from_slug():
    payload = {"jobs": [{"title": "Eng", "slug": "eng-role", "location": "Remote EU"}]}
    assert _fields(fetch("homerun", "acme", FakeClient(payload))) == [
        ("Eng", "https://acme.homerun.co/eng-role", "Remote EU", None),
    ]


def test_breezy_builds_url_from_slug_with_p_segment():
    payload = [{"name": "Eng", "slug": "eng-1", "location": {"name": "Remote"}}]
    assert _fields(fetch("breezy", "acme", FakeClient(payload))) == [
        ("Eng", "https://acme.breezy.hr/p/eng-1", "Remote", None),
    ]


def test_workable_widget_api():
    payload = {
        "jobs": [
            {
                "title": "MLOps",
                "url": "https://apply.workable.com/acme/j/AB",
                "location": "Remote",
                "published": "2026-05-05",
            }
        ]
    }
    client = FakeClient(payload)
    assert _fields(fetch("workable", "acme", client)) == [
        ("MLOps", "https://apply.workable.com/acme/j/AB", "Remote", "2026-05-05"),
    ]
    assert client.urls == ["https://apply.workable.com/api/v1/widget/accounts/acme?details=true"]


def test_smartrecruiters_paginates_and_builds_url():
    page = {"content": [{"name": "AI Eng", "id": "123", "location": {"city": "Remote"}, "releasedDate": "2026-06-06"}]}

    class Paged(FakeClient):
        def request_json(self, url, method="GET", json_body=None, timeout=None):
            self.urls.append(url)
            return page if len(self.urls) == 1 else {"content": []}

    assert _fields(fetch("smartrecruiters", "acme", Paged())) == [
        ("AI Eng", "https://careers.smartrecruiters.com/acme/123", "Remote", "2026-06-06"),
    ]


def test_smartrecruiters_empty_first_page_fails_closed():
    with pytest.raises(AtsClientError, match="board_not_found"):
        fetch("smartrecruiters", "nonexistent-slug", FakeClient({"offset": 0, "totalFound": 0, "content": []}))


def test_workable_merges_location_rows_for_the_same_posting():
    job = {"title": "Support Specialist", "url": "https://apply.workable.com/j/1234AB", "telecommuting": True,
           "published_on": "2026-09-16", "created_at": "2026-09-12"}
    rows = [dict(job, locations=[{"country": country, "hidden": hidden}])
            for country, hidden in [("Poland", False), ("United Kingdom", False), ("Ireland", True)]]
    posting, = fetch("workable", "acme", FakeClient({"jobs": rows}))
    assert (posting.location, posting.remote_hint, posting.posted_at) == (
        "Poland; United Kingdom", "Remote", "2026-09-16",
    )


def test_smartrecruiters_uses_full_location_and_explicit_workplace():
    rows = [
        {"name": "Engineer", "id": "123", "location": {
            "city": "Bundoora", "country": "au", "fullLocation": "Bundoora, Victoria, Australia", "hybrid": True,
        }},
        {"name": "Technician", "id": "456", "location": {"city": "Madrid", "remote": True}},
        {"name": "Technician", "id": "678", "department": {"name": "Operations"}},
    ]
    postings = fetch("smartrecruiters", "acme", FakeClient({"content": rows}))
    assert [(p.location, p.remote_hint) for p in postings] == [
        ("Bundoora, Victoria, Australia", "Hybrid"), ("Madrid", "Remote"), ("", ""),
    ]


@pytest.mark.parametrize("vendor", ["recruitee", "teamtailor", "homerun", "breezy", "workable"])
def test_empty_payload_yields_no_postings(vendor):
    assert fetch(vendor, "acme", FakeClient({})) == []


def test_personio_parses_xml_and_builds_job_url():
    xml = (
        '<?xml version="1.0"?><workzag-jobs>'
        "<position><id>7</id><name>Engineer</name><office>Berlin</office><createdAt>2026-01-01</createdAt></position>"
        "<position><id>8</id><name></name></position></workzag-jobs>"
    )
    out = fetch("personio", "acme", FakeClient(text_value=xml))
    assert _fields(out) == [("Engineer", "https://acme.jobs.personio.de/job/7", "Berlin", "2026-01-01")]


def test_personio_rejects_xml_entity_bombs():
    bomb = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
        "<workzag-jobs>&b;</workzag-jobs>"
    )
    with pytest.raises(AtsClientError):
        fetch("personio", "acme", FakeClient(text_value=bomb))


def test_personio_transport_failure_raises():
    with pytest.raises(AtsClientError):
        fetch("personio", "acme", FakeClient(text_value=RuntimeError("down")))


def test_jobvite_parses_html():
    html = 'x jv-job-list <a href="https://jobs.jobvite.com/acme/job/1" data-title="Dev" data-location="Madrid">'
    out = fetch("jobvite", "acme", FakeClient(text_value=html))
    assert _fields(out) == [("Dev", "https://jobs.jobvite.com/acme/job/1", "Madrid", None)]


def test_jazzhr_parses_listing():
    html = 'junk job-listing <a href="/apply/1/dev">x</a><span class="job-title">Dev</span>'
    out = fetch("jazzhr", "acme", FakeClient(text_value=html))
    assert [(p.title, p.apply_url) for p in out] == [("Dev", "https://acme.applytojob.com/apply/1/dev")]


def test_jazzhr_preserves_absolute_href():
    html = 'junk job-listing <a href="https://acme.applytojob.com/apply/2/ml">x</a><span class="job-title">ML</span>'
    out = fetch("jazzhr", "acme", FakeClient(text_value=html))
    assert [(p.title, p.apply_url) for p in out] == [("ML", "https://acme.applytojob.com/apply/2/ml")]


def test_bamboohr_json_api():
    payload = {"result": [{"id": 5, "jobOpeningName": "Dev", "location": {"city": "Madrid"}}]}
    out = fetch("bamboohr", "acme", FakeClient(payload))
    assert _fields(out) == [("Dev", "https://acme.bamboohr.com/careers/5", "Madrid", None)]


def test_bamboohr_html_fallback():
    html = (
        '<div><a class="BambooHR-ATS-Jobs-Item-Title" href="/careers/9">QA</a>'
        '<span class="BambooHR-ATS-Jobs-Item-Location">Remote</span></div>'
    )
    out = fetch("bamboohr", "acme", FakeClient({}, html))
    assert _fields(out) == [("QA", "https://acme.bamboohr.com/careers/9", "Remote", None)]


def test_factorial_json_api():
    payload = {"jobs": [{"title": "Dev", "url": "https://jobs.factorialhr.com/acme/1"}]}
    assert len(fetch("factorial", "acme", FakeClient(payload))) == 1


def test_pinpoint_json_api():
    payload = {"jobs": [{"title": "Dev", "url": "https://acme.pinpointhq.com/postings/1", "location": "Remote"}]}
    assert _fields(fetch("pinpoint", "acme", FakeClient(payload)))[0][0] == "Dev"


def test_comeet_parses_positions():
    html = '<a href="/jobs/acme/AB.123/CD.456/dev-role">Dev Role</a><a href="/jobs/acme/x">skip</a>'
    out = fetch("comeet", "acme", FakeClient(text_value=html))
    assert [(p.title, p.apply_url) for p in out] == [
        ("Dev Role", "https://www.comeet.co/jobs/acme/AB.123/CD.456/dev-role")
    ]


def test_icims_paginates_until_empty():
    html = (
        '<a class="iCIMS_JobsTable_title" href="/jobs/1/dev">Dev</a>'
        '<span class="iCIMS_JobsTable_location">Madrid</span>'
    )

    class Paged(FakeClient):
        def request_text(self, url, timeout=None):
            self.urls.append(url)
            return html if "pr=0" in url else ""

    out = fetch("icims", "careers-acme.icims.com", Paged())
    assert _fields(out) == [("Dev", "https://careers-acme.icims.com/jobs/1/dev", "Madrid", None)]


def test_taleo_parses_cards():
    html = '<a class="jobTitle-link" href="/job/1">Dev</a>'
    out = fetch("taleo", "acme.taleo.net", FakeClient(text_value=html))
    assert [(p.title, p.apply_url) for p in out] == [("Dev", "https://acme.taleo.net/job/1")]


@pytest.mark.parametrize("vendor", ["icims", "taleo"])
def test_browser_only_fetch_failure_is_an_error_not_an_empty_board(vendor):
    with pytest.raises(AtsClientError, match="browser_fallback_unavailable"):
        fetch(vendor, HOST_BOARDS[vendor], FakeClient(text_value=RuntimeError("down")))


@pytest.mark.parametrize("vendor", ["workday", "icims", "taleo", "comeet"])
def test_invalid_locator_still_raises(vendor):
    with pytest.raises(AtsClientError):
        fetch(vendor, "", FakeClient())


def test_base_helpers():
    assert locator_url("acme.io", "a", "/b/") == "https://acme.io/a/b"
    assert locator_has_host("a.b/c") and not locator_has_host("acme")
    assert extract_between("x[abc]y", "[", "]") == "abc"
    assert parse_card_listings('<a class="t" href="/x">T</a>', "t") == [{"title": "T", "href": "/x", "location": ""}]


@pytest.mark.parametrize("vendor", ["comeet", "icims", "taleo", "lever", "personio", "jobvite"])
def test_budget_stop_propagates_from_broad_catch_clients(vendor):
    stop = RequestBudgetExceeded("spent", scope="host", host="x")

    class Budget(FakeClient):
        def request_json(self, url, method="GET", json_body=None, timeout=None):
            raise stop

        def request_text(self, url, timeout=None):
            raise stop

    with pytest.raises(RequestBudgetExceeded) as info:
        fetch(vendor, HOST_BOARDS.get(vendor, "acme"), Budget())
    assert info.value.scope == "host"


def test_teamtailor_falls_back_to_jobs_rss_on_404():
    from ultron.net import HttpError

    rss = """<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:tt="https://teamtailor.com/locations"><channel>
      <item>
        <title>Ingeniero de Procesos</title>
        <link>https://ingeteam.teamtailor.com/jobs/123-proceso</link>
        <pubDate>Fri, 13 Nov 2026 10:00:00 +0100</pubDate>
        <remoteStatus>fully</remoteStatus>
        <tt:locations><tt:location><tt:city>Bilbao</tt:city><tt:country>Spain</tt:country></tt:location></tt:locations>
      </item>
    </channel></rss>"""

    class RssFallbackClient(FakeClient):
        def request_json(self, url, method="GET", json_body=None, timeout=None):
            self.urls.append(url)
            raise HttpError("HTTP 404", status_code=404)

    out = fetch("teamtailor", "ingeteam", RssFallbackClient(text_value=rss))
    assert len(out) == 1
    assert out[0].title == "Ingeniero de Procesos"
    assert out[0].apply_url == "https://ingeteam.teamtailor.com/jobs/123-proceso"
    assert out[0].location == "Bilbao, Spain"
    assert out[0].remote_hint == "Remote"


def test_teamtailor_rss_rejects_xml_entity_bombs():
    from ultron.net import HttpError

    bomb = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;">]>'
        "<rss><item><title>&b;</title><link>https://x.teamtailor.com/jobs/1</link></item></rss>"
    )

    class RssBombClient(FakeClient):
        def request_json(self, url, method="GET", json_body=None, timeout=None):
            raise HttpError("HTTP 404", status_code=404)

    with pytest.raises(AtsClientError, match="teamtailor_invalid_rss"):
        fetch("teamtailor", "acme", RssBombClient(text_value=bomb))


def test_workable_null_title_or_url_is_skipped():
    payload = {
        "jobs": [
            {"title": "MLOps", "url": None},
            {"title": None, "url": "https://apply.workable.com/acme/j/CD"},
            {"title": "Analyst", "url": "https://apply.workable.com/acme/j/EF"},
        ]
    }
    out = fetch("workable", "acme", FakeClient(payload))
    assert [(p.title, p.apply_url) for p in out] == [("Analyst", "https://apply.workable.com/acme/j/EF")]
