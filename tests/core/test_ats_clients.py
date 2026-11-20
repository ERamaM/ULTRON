from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ultron.core.ats.clients import FETCHERS, AtsClientError, fetch_vendor_jobs
from ultron.core.ats.clients.ashby import fetch_ashby_jobs
from ultron.core.ats.clients.greenhouse import fetch_greenhouse_jobs
from ultron.core.ats.clients.lever import fetch_lever_jobs
from ultron.net import HttpClient, HttpError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ats"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[str, str, object]] = []

    def request_json(self, url, method="GET", json_body=None, timeout=None):
        self.calls.append((url, method, json_body))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request_text(self, url, timeout=None):  # pragma: no cover
        raise AssertionError("unused")


def fields(postings):
    return [(p.title, p.apply_url, p.location, p.posted_at) for p in postings]


def test_greenhouse_parses_and_canonicalizes_urls():
    client = FakeClient([load("greenhouse_board.json")])
    out = fetch_greenhouse_jobs("acme", client=client)
    assert client.calls[0][0] == "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false"
    assert fields(out) == [
        (
            "Senior Data Scientist",
            "https://boards.greenhouse.io/acme/jobs/4012345",
            "Remote - Spain",
            "2026-10-20T10:15:00-04:00",
        ),
        ("ML Engineer", "https://boards.greenhouse.io/acme/jobs/4012346", "Madrid, Spain", "2026-10-18T09:00:00-04:00"),
        ("Analytics Lead", "https://boards.greenhouse.io/acme/jobs/4012347", "Barcelona", "2026-09-01T08:00:00-04:00"),
    ]


def test_greenhouse_non_dict_payload_is_empty():
    assert fetch_greenhouse_jobs("acme", client=FakeClient([[]])) == []


def test_greenhouse_uses_first_publication_over_last_edit():
    payload = {"jobs": [{"title": "Engineer", "id": 123, "first_published": "2026-09-10",
                         "updated_at": "2026-11-13"}]}
    assert fetch_greenhouse_jobs("acme", client=FakeClient([payload]))[0].posted_at == "2026-09-10"


def test_greenhouse_falls_back_to_eu_host():
    client = FakeClient([HttpError("HTTP 404", status_code=404), load("greenhouse_board.json")])
    out = fetch_greenhouse_jobs("acme", client=client)
    assert [c[0] for c in client.calls] == [
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=false",
        "https://boards-api.eu.greenhouse.io/v1/boards/acme/jobs?content=false",
    ]
    assert out[0].apply_url == "https://boards.eu.greenhouse.io/acme/jobs/4012345"



def test_greenhouse_keeps_the_primary_404_when_the_eu_fallback_cannot_resolve():
    # A board removed from Greenhouse answers 404 on the primary API; the EU fallback host may not resolve.
    # The reported error must stay the 404, not the fallback's dns_resolution_failed.
    from ultron.net import URLRejected

    client = FakeClient([HttpError("HTTP 404", status_code=404), URLRejected("dns_resolution_failed")])
    with pytest.raises(HttpError) as excinfo:
        fetch_greenhouse_jobs("acme", client=client)
    assert excinfo.value.status_code == 404


def test_greenhouse_policy_rejection_of_the_primary_host_is_not_hidden():
    from ultron.net import URLRejected

    with pytest.raises(URLRejected):
        fetch_greenhouse_jobs("acme", client=FakeClient([URLRejected("domain_not_allowed")]))

def test_lever_parses_board():
    out = fetch_lever_jobs("acme", client=FakeClient([load("lever_board.json")]))
    assert fields(out) == [
        ("Machine Learning Engineer", "https://jobs.lever.co/acme/a1", "Madrid", "1760000000000"),
        ("Data Analyst", "https://jobs.lever.co/acme/a2", "Remote - EU", "1759000000000"),
        ("Product Designer", "https://jobs.lever.co/acme/a3", "", None),
    ]


def test_lever_keeps_all_locations_and_workplace_from_a_large_board():
    payload = [{
        "text": "Support Engineer", "hostedUrl": "https://jobs.lever.co/acme/1234",
        "categories": {"location": "Toronto", "allLocations": ["Toronto", "London"]},
        "workplaceType": "hybrid", "description": "x" * 2048,
    }]
    with HttpClient(
        max_requests=2, max_requests_per_host=2, max_bytes=1024, min_interval_per_host=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        resolver=lambda host, port: ["93.184.216.34"],
    ) as client:
        posting, = fetch_vendor_jobs("lever", "acme", client=client, timeout=5)
    assert (posting.location, posting.remote_hint) == ("Toronto; London", "hybrid")


def test_lever_empty_board_returns_empty_not_error():
    assert fetch_lever_jobs("acme", client=FakeClient([[], []])) == []


def test_lever_transport_failure_still_raises():
    client = FakeClient([HttpError("boom", status_code=500), HttpError("boom", status_code=500)])
    with pytest.raises(HttpError):
        fetch_lever_jobs("acme", client=client)


def test_lever_falls_back_to_eu_host():
    client = FakeClient([HttpError("HTTP 500", status_code=500), load("lever_board.json")])
    out = fetch_lever_jobs("acme", client=client)
    assert [c[0] for c in client.calls] == [
        "https://api.lever.co/v0/postings/acme?mode=json",
        "https://api.eu.lever.co/v0/postings/acme?mode=json",
    ]
    assert len(out) == 3


def test_lever_falls_back_through_real_http_client():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.lever.co":
            return httpx.Response(500, text="err")
        return httpx.Response(200, json=load("lever_board.json"))

    client = HttpClient(
        max_requests=50,
        max_requests_per_host=50,
        min_interval_per_host=0.0,
        transport=httpx.MockTransport(handler),
        resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None,
    )
    assert len(fetch_lever_jobs("acme", client=client)) == 3


def test_ashby_uses_public_api_and_skips_unlisted():
    client = FakeClient([load("ashby_board.json")])
    out = fetch_ashby_jobs("acme", client=client)
    assert client.calls == [("https://api.ashbyhq.com/posting-api/job-board/acme", "GET", None)]
    assert [p.title for p in out] == ["AI Engineer", "Data Engineer"]
    assert [p.location for p in out] == ["Remote", "Madrid"]
    assert [p.remote_hint for p in out] == ["Remote", "Hybrid"]
    assert out[0].posted_at == "2026-10-10T12:00:00.000+00:00"


@pytest.mark.parametrize("payload", [{"data": None}, {"jobs": None}, []])
def test_ashby_reports_unexpected_payload(payload):
    with pytest.raises(AtsClientError, match="unexpected_payload"):
        fetch_ashby_jobs("acme", client=FakeClient([payload]))


def test_ashby_skips_archived_jobs_even_when_listed():
    payload = {"jobs": [
        {"title": "ML Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/1", "isListed": True},
        {"title": "Archived", "jobUrl": "https://jobs.ashbyhq.com/acme/2", "isListed": True, "isArchived": True},
        {"title": "Archived flag only", "jobUrl": "https://jobs.ashbyhq.com/acme/3", "isArchived": True},
        {"title": "Explicitly active", "jobUrl": "https://jobs.ashbyhq.com/acme/4", "isArchived": False},
    ]}
    titles = [p.title for p in fetch_ashby_jobs("acme", client=FakeClient([payload]))]
    assert titles == ["ML Engineer", "Explicitly active"]


def test_ashby_empty_jobs_list_is_valid_empty_board():
    assert fetch_ashby_jobs("acme", client=FakeClient([{"jobs": []}])) == []


def test_ashby_includes_distinct_secondary_locations():
    payload = load("ashby_board.json")
    payload["jobs"][1]["secondaryLocations"] = [{"location": "Madrid"}, {"location": "Lisbon"}, {}, None]
    assert fetch_ashby_jobs("acme", client=FakeClient([payload]))[1].location == "Madrid; Lisbon"


def test_ashby_board_has_a_larger_response_limit():
    payload = load("ashby_board.json")
    payload["jobs"][0]["descriptionPlain"] = "x" * 2048
    with HttpClient(
        max_requests=2, max_requests_per_host=2, max_bytes=1024, min_interval_per_host=0,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload)),
        resolver=lambda host, port: ["93.184.216.34"],
    ) as client:
        with pytest.raises(HttpError, match="not valid JSON"):
            client.request_json("https://api.ashbyhq.com/posting-api/job-board/acme")
        jobs = fetch_vendor_jobs("ashby", "acme", client=client, timeout=5)

    assert len(jobs) == 2
    assert "descriptionPlain" not in jobs[0].raw_payload


def test_fetch_vendor_jobs_dispatches():
    assert {"greenhouse", "lever", "ashby"} <= set(FETCHERS)
    out = fetch_vendor_jobs("ashby", "acme", client=FakeClient([load("ashby_board.json")]), timeout=5)
    assert len(out) == 2


def test_unsupported_vendor_raises():
    with pytest.raises(AtsClientError, match="vendor_not_supported_yet"):
        fetch_vendor_jobs("nosuchvendor", "acme", client=FakeClient([]), timeout=5)


def test_greenhouse_null_title_or_location_is_not_stringified():
    payload = {
        "jobs": [
            {"id": 1, "title": None, "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"},
            {
                "id": 2,
                "title": "Engineer",
                "location": {"name": None},
                "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
            },
        ]
    }
    out = fetch_greenhouse_jobs("acme", client=FakeClient([payload]))
    assert [(p.title, p.location) for p in out] == [("Engineer", "")]


def test_lever_null_hosted_url_or_title_is_skipped():
    payload = [
        {"text": "Engineer", "hostedUrl": None},
        {"text": None, "hostedUrl": "https://jobs.lever.co/acme/2"},
        {"text": "Analyst", "hostedUrl": "https://jobs.lever.co/acme/3"},
    ]
    out = fetch_lever_jobs("acme", client=FakeClient([payload]))
    assert [(p.title, p.apply_url) for p in out] == [("Analyst", "https://jobs.lever.co/acme/3")]
