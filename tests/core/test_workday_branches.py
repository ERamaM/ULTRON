from __future__ import annotations

import json

import httpx
import pytest

from ultron.core.ats.clients import workday
from ultron.core.ats.clients.base import AtsClientError
from ultron.infra.net import HttpClient, RequestBudgetExceeded


def _job(index: int) -> dict[str, str]:
    return {"title": f"Role {index}", "externalPath": f"/job/{index}", "locationsText": "Remote"}


class FakeClient:
    def __init__(self, json_responses=(), text=None):
        self.json_responses = iter(json_responses)
        self.text = text
        self.requests = []

    def request_json(self, url, method="GET", json_body=None, timeout=None):
        self.requests.append(json_body)
        value = next(self.json_responses)
        if isinstance(value, Exception):
            raise value
        return value

    def request_text(self, url, timeout=None):
        if isinstance(self.text, Exception):
            raise self.text
        return self.text


def _http_client(handler, max_requests: int) -> HttpClient:
    return HttpClient(
        max_requests=max_requests,
        max_requests_per_host=max_requests,
        min_interval_per_host=0.0,
        transport=httpx.MockTransport(handler),
        resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None,
    )


def test_workday_locator_parsing_and_validation():
    assert workday._split_locator("") == ("", "")
    assert workday._split_locator("wd.example") == ("wd.example", "")
    assert workday._split_locator("wd.example/External/Acme") == ("wd.example", "External/Acme")
    assert workday._split_locator("https://wd.example/External/Acme") == ("wd.example", "External/Acme")

    with pytest.raises(AtsClientError):
        workday.fetch_workday_jobs("/External", client=FakeClient())


def test_workday_paginates_and_preserves_partial_results_after_later_error():
    client = FakeClient([{"jobPostings": [_job(i) for i in range(20)]}, RuntimeError("second page failed")])
    postings = workday.fetch_workday_jobs("wd.example/External/Acme", client=client)

    assert len(postings) == 20
    assert postings[-1].apply_url.endswith("/job/19")


def test_workday_uses_next_data_when_api_is_empty():
    payload = {"props": {"pageProps": {"jobs": [{"title": "Fallback", "externalPath": "/job/fallback"}]}}}
    html = f'<script id="__NEXT_DATA__">{json.dumps(payload)}</script>'
    client = FakeClient([{"jobPostings": []}], text=html)
    postings = workday.fetch_workday_jobs("wd.example/External/Acme", client=client)
    assert [posting.title for posting in postings] == ["Fallback"]


def test_workday_page_fetch_failure_is_an_error_without_a_browser():
    client = FakeClient([{"jobPostings": []}], text=RuntimeError("down"))
    with pytest.raises(AtsClientError, match="browser_fallback_unavailable"):
        workday.fetch_workday_jobs("wd.example/External/Acme", client=client)


def test_workday_respects_page_cap():
    posts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        if request.method == "POST":
            posts += 1
            return httpx.Response(200, json={"jobPostings": [_job(posts * 100 + i) for i in range(20)]})
        return httpx.Response(404)

    client = _http_client(handler, max_requests=500)
    out = workday.fetch_workday_jobs("wd.example/External/Acme", client=client)
    assert posts == 100  # MARK III safety cap (_WD_MAX_PAGES)
    assert len(out) == 2000
    assert client.requests_made <= client.max_requests


def test_workday_budget_stop_propagates_instead_of_partial_results():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"jobPostings": [_job(i) for i in range(20)]})

    client = _http_client(handler, max_requests=7)
    with pytest.raises(RequestBudgetExceeded) as info:
        workday.fetch_workday_jobs("wd.example/External/Acme", client=client)
    assert info.value.scope == "global"
    assert client.requests_made == 7


@pytest.mark.parametrize("status", [404, 422])
def test_workday_initial_cxs_client_error_propagates_instead_of_empty_list(status):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(status, json={"error": "not found"})
        return httpx.Response(200, text="<html><body>Workday shell</body></html>")

    from ultron.infra.net import HttpError

    client = _http_client(handler, max_requests=10)
    with pytest.raises(HttpError) as info:
        workday.fetch_workday_jobs("acme.wd5.myworkdayjobs.com/WrongSite", client=client)
    assert info.value.status_code == status


def test_workday_apply_url_keeps_site_path_when_external_path_lacks_it():
    client = FakeClient([
        {
            "jobPostings": [
                {"title": "Missing Site", "externalPath": "/job/Madrid/ML-Engineer_R-1", "locationsText": "Madrid"},
                {"title": "Has Site", "externalPath": "/External/job/Madrid/Data-Eng_R-2", "locationsText": "Remote"},
            ]
        }
    ])
    postings = workday.fetch_workday_jobs("acme.wd5.myworkdayjobs.com/External", client=client)
    assert [p.apply_url for p in postings] == [
        "https://acme.wd5.myworkdayjobs.com/External/job/Madrid/ML-Engineer_R-1",
        "https://acme.wd5.myworkdayjobs.com/External/job/Madrid/Data-Eng_R-2",
    ]

    next_data = {"props": {"pageProps": {"jobs": [{"title": "SSR", "externalPath": "/job/Madrid/SSR_R-3"}]}}}
    html = f'<script id="__NEXT_DATA__">{json.dumps(next_data)}</script>'
    fallback_client = FakeClient([{"jobPostings": []}], text=html)
    ssr = workday.fetch_workday_jobs("acme.wd3.myworkdayjobs.com/en-US/Careers", client=fallback_client)
    assert ssr[0].apply_url == "https://acme.wd3.myworkdayjobs.com/en-US/Careers/job/Madrid/SSR_R-3"


def test_workday_cxs_403_falls_back_to_next_data_postings():
    next_data = {"props": {"pageProps": {"jobs": [{"title": "WAF Bypassed SSR", "externalPath": "/job/1"}]}}}
    html = f'<script id="__NEXT_DATA__">{json.dumps(next_data)}</script>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, text=html)

    client = _http_client(handler, max_requests=10)
    postings = workday.fetch_workday_jobs("wd3.myworkdaysite.com/recruiting/acme/External", client=client)
    assert len(postings) == 1
    assert postings[0].title == "WAF Bypassed SSR"
    assert postings[0].apply_url == "https://wd3.myworkdaysite.com/recruiting/acme/External/job/1"


def test_workday_cxs_transport_error_falls_back_to_next_data_postings():
    from ultron.infra.net import HttpError

    next_data = {"props": {"pageProps": {"jobs": [{"title": "Recovered SSR", "externalPath": "/job/2"}]}}}
    html = f'<script id="__NEXT_DATA__">{json.dumps(next_data)}</script>'
    client = FakeClient([HttpError("timeout", status_code=None)], text=html)
    postings = workday.fetch_workday_jobs("acme.wd5.myworkdayjobs.com/External", client=client)
    assert [p.title for p in postings] == ["Recovered SSR"]


def test_workday_cxs_error_reraises_original_when_html_also_fails():
    from ultron.infra.net import HttpError

    orig = HttpError("HTTP 404", status_code=404)
    client = FakeClient([orig], text=HttpError("HTML 502", status_code=502))
    with pytest.raises(HttpError) as info:
        workday.fetch_workday_jobs("acme.wd5.myworkdayjobs.com/WrongSite", client=client)
    assert info.value is orig
    assert info.value.status_code == 404


def test_workday_host_budget_returns_partial_results_and_max_pages_caps():
    calls = 0

    class HostCapClient:
        def request_json(self, url, method="GET", json_body=None, timeout=None):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RequestBudgetExceeded("host limit", scope="host", host="acme.wd5.myworkdayjobs.com")
            return {"jobPostings": [_job(i) for i in range(20)]}

        def request_text(self, url, timeout=None):
            return ""

    out = workday.fetch_workday_jobs("acme.wd5.myworkdayjobs.com/External", client=HostCapClient())
    assert len(out) == 20

    calls = 0
    capped = workday.fetch_workday_jobs("acme.wd5.myworkdayjobs.com/External", client=HostCapClient(), max_pages=1)
    assert len(capped) == 20 and calls == 1


def test_workday_interleaves_preferred_country_with_unfiltered_pages():
    broad_page = [_job(i) for i in range(20)]
    client = FakeClient([
        {
            "total": 21,
            "jobPostings": broad_page,
            "facets": [{"facetParameter": "locationMainGroup", "values": [
                {"facetParameter": "locationCountry", "values": [
                    {"id": "es-id", "descriptor": "España"}, {"id": "us-id", "descriptor": "United States"},
                ]},
            ]}],
        },
        {"total": 2, "jobPostings": [_job(0), _job(99)]},
        {"total": 21, "jobPostings": [_job(20)]},
    ])
    postings = workday.fetch_workday_jobs(
        "acme.wd5.myworkdayjobs.com/External", client=client, preferred_locations=("Espana", "Europe")
    )
    assert [(req["offset"], req["appliedFacets"]) for req in client.requests] == [
        (0, {}), (0, {"locationCountry": ["es-id"]}), (20, {}),
    ]
    assert [posting.title for posting in postings] == [*(f"Role {i}" for i in range(20)), "Role 99", "Role 20"]


def test_workday_rejected_country_facet_keeps_unfiltered_pagination():
    from ultron.infra.net import HttpError

    client = FakeClient([
        {
            "total": 21,
            "jobPostings": [_job(i) for i in range(20)],
            "facets": [{"facetParameter": "locationCountry", "values": [{"id": "es-id", "descriptor": "Spain"}]}],
        },
        HttpError("unsupported facet", status_code=400),
        {"total": 21, "jobPostings": [_job(20)]},
    ])
    postings = workday.fetch_workday_jobs(
        "acme.wd5.myworkdayjobs.com/External", client=client, preferred_locations=("Spain",)
    )
    assert len(postings) == 21 and postings[-1].title == "Role 20"


def test_next_data_extractor_survives_hostile_deeply_nested_json():
    html = '<script id="__NEXT_DATA__" type="application/json">' + "[" * 200000 + "</script>"
    assert workday._extract_from_next_data(html, "acme.wd5.myworkdayjobs.com") == []


def test_next_data_extractor_handles_nesting_deeper_than_the_recursion_limit(monkeypatch):
    # Build the structure directly: how deep json.loads itself can go varies by Python build.
    deep: list = []
    for _ in range(5000):
        deep = [deep]
    payload = {"props": {"jobs": [_job(1)]}, "deep": deep}
    monkeypatch.setattr(workday, "load_json_or_none", lambda text: payload)
    html = '<script id="__NEXT_DATA__" type="application/json">{}</script>'
    postings = workday._extract_from_next_data(html, "acme.wd5.myworkdayjobs.com")
    assert [p.title for p in postings] == ["Role 1"]


def test_shared_host_cxs_requests_name_each_recruiting_tenant():
    from ultron.core.ats.clients import fetch_vendor_jobs

    seen = []

    def handler(request):
        seen.append(request.url.path)
        tenant = request.url.path.split("/")[3]
        return httpx.Response(200, json={"jobPostings": [{"title": tenant, "externalPath": "/job/1"}]})

    client = _http_client(handler, max_requests=10)
    for tenant in ("north", "south"):
        board = f"wd3.myworkdaysite.com/recruiting/{tenant}/External"
        postings = fetch_vendor_jobs("workday", board, client=client, timeout=1)
        assert [posting.title for posting in postings] == [tenant]
    assert seen == ["/wday/cxs/north/External/jobs", "/wday/cxs/south/External/jobs"]
