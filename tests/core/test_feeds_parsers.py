from __future__ import annotations

from pathlib import Path

import pytest

from ultron.core.feeds import collectors, parsers, urls
from ultron.net import RequestBudgetExceeded

FEEDS = Path(__file__).resolve().parents[1] / "fixtures" / "feeds"


class FakeClient:
    """Stands in for HttpClient: responses are served in order (dict/list for json, str for text)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requested: list[str] = []

    def _next(self, url):
        self.requested.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request_json(self, url, method="GET", json_body=None, timeout=None):
        return self._next(url)

    def request_text(self, url, timeout=None):
        return self._next(url)


def test_collectors_parse_json_and_paginate():
    c = FakeClient(
        {"data": [{"title": "ML Engineer", "url": "https://a/jobs/1"}], "links": {"next": "next"}},
        {"data": [{"title": "Data Engineer", "url": "https://a/jobs/2"}], "links": {}},
    )
    rows, discards = collectors.collect_arbeitnow_urls(1, 2, api_url="https://a/api", request_json=c.request_json)
    assert len(rows) == 2 and not discards and c.requested == ["https://a/api", "next"]

    himalayas = FakeClient({"jobs": [{"title": "H", "company": {"name": "Acme"}, "url": "https://h/jobs"}]})
    rows, _ = collectors.collect_himalayas_urls(1, 1, api_url="u", request_json=himalayas.request_json)
    assert rows[0]["company_name"] == "Acme"
    remotive = FakeClient({"jobs": [{"title": "R", "company_name": "Beta", "url": "https://r/jobs", "description": "remote"}]})
    assert collectors.collect_remotive_urls(1, api_url="u", request_json=remotive.request_json)[0][0]["description"] == "remote"
    nomads = FakeClient([{"title": "N", "company_name": "Gamma", "url": "https://n/jobs"}])
    assert collectors.collect_workingnomads_urls(1, api_url="u", request_json=nomads.request_json)[0][0]["company_name"] == "Gamma"
    jobicy = FakeClient({"jobs": [{"jobTitle": "J", "companyName": "Delta", "url": "https://j/jobs"}]})
    assert collectors.collect_jobicy_api_urls(1, api_url="u", request_json=jobicy.request_json)[0][0]["source"] == "jobicy_api"
    okok = FakeClient([{"position": "R", "company": "Eps", "url": "https://o/jobs"}])
    assert collectors.collect_remoteok_urls(1, api_url="u", request_json=okok.request_json)[0][0]["source"] == "remoteok"
    muse = FakeClient(
        {"results": [{"name": "M", "company": {"name": "Zeta"}, "refs": {"landing_page": "https://m/jobs"}}], "page_count": 1}
    )
    assert collectors.collect_themuse_urls(1, 1, api_url="u", request_json=muse.request_json)[0][0]["company_name"] == "Zeta"


def test_collectors_parse_rss_markdown_hn_and_html():
    rss = "<rss><channel><item><title>Engineer at Acme</title><guid>https://acme/jobs</guid></item></channel></rss>"
    atom = (FEEDS / "atom.xml").read_text(encoding="utf-8")
    c = FakeClient(rss, atom, "| [Acme](https://acme.example/jobs) |")
    first = collectors.collect_rss_urls("https://rss", "rss", 1, max_items=5, request_text=c.request_text)
    assert first[0][0]["company_name"] == "Acme"
    second = collectors.collect_rss_urls("https://atom", "atom", 1, max_items=5, request_text=c.request_text)
    assert second[0][0]["company_name"] == "Beta"
    github = collectors.collect_github_awesome_companies(1, url="u", request_text=c.request_text)
    assert github[0][0]["company_name"] == "Acme"
    hn = FakeClient({"hits": [{"objectID": "123"}]}, {"children": [{"text": "Beta | Remote | Engineer | https://beta.example/jobs"}]})
    rows, _ = collectors.collect_hn_who_is_hiring(
        1, max_comments=5, request_json=hn.request_json, search_url="https://hn/search", items_url="https://hn/items/"
    )
    assert rows[0]["company_name"] == "Beta" and rows[0]["url"] == "https://beta.example/jobs"
    assert hn.requested[1] == "https://hn/items/123"
    html = FakeClient("<main><li>Acme Corp</li></main>", "", "")
    top = collectors.collect_weworkremotely_top_company_hints(1, urls=("u",), max_companies=5, request_text=html.request_text)
    assert top[0][0]["company_name"] == "Acme Corp"
    assert collectors.collect_remote_es_company_hints(1, url="u", request_text=html.request_text)[0] == []
    assert collectors.collect_remotifyeurope_company_hints(1, url="u", request_text=html.request_text)[0] == []


def test_collector_failure_becomes_discard_but_budget_propagates():
    rows, discards = collectors.collect_remotive_urls(
        1, api_url="https://r/api", request_json=FakeClient(RuntimeError("boom")).request_json
    )
    assert rows == [] and discards[0]["detail"].startswith("remotive:")
    budget = FakeClient(RequestBudgetExceeded("x", scope="global"))
    with pytest.raises(RequestBudgetExceeded):
        collectors.collect_remotive_urls(1, api_url="u", request_json=budget.request_json)


def test_parsers_edge_cases():
    assert parsers.split_title_company("Acme: ML Engineer", "weworkremotely") == ("ML Engineer", "Acme")
    assert parsers.split_title_company("ML Engineer at Acme", "x") == ("ML Engineer", "Acme")
    assert parsers.split_title_company("", "x") == ("", "")
    assert parsers.rows_from_rss_payload("not xml", "x") == []
    assert parsers.rows_from_remoteok_payload({}) == []
    assert parsers.rows_from_themuse_payload([]) == ([], None)
    entries = parsers.extract_company_entries_from_remote_es_markdown((FEEDS / "remote_es.md").read_text(encoding="utf-8"))
    assert entries == [("Acme Corp", "https://boards.greenhouse.io/acmecorp"), ("Nimbus", "https://nimbus.example/careers")]


def test_weworkremotely_top_extracts_company_spans_and_ignores_nav():
    html = (
        "<header><nav><a href='/categories/programming'>Programming</a><a href='/jobs'>Find Jobs</a></nav></header>"
        "<main><a href='/company/lumenalta'><span class='rank'>1</span><span class='company'>Lumenalta</span>"
        "<span class='job-count'>Jobs Posted: 573</span></a>"
        "<a href='/company/toptal'><span class='company'>Toptal</span></a></main>"
        "<footer><a href='/design'>All Design Jobs</a></footer>"
    )
    assert parsers.extract_company_names_from_weworkremotely_top_html(html) == ["Lumenalta", "Toptal"]
    html = (FEEDS / "remotifyeurope.html").read_text(encoding="utf-8")
    assert parsers.extract_company_names_from_remotifyeurope_html(html, ["germany", "spain"]) == ["Contoso"]


def test_url_utils_and_page_links():
    wrapped = "https://x/r?url=https%3A%2F%2Fboards.greenhouse.io%2Facme%2Fjobs%2F1"
    assert urls.candidate_urls_from_row({"url": wrapped}) == [wrapped, "https://boards.greenhouse.io/acme/jobs/1"]
    assert urls.looks_like_company_jobs_url("https://acme.example/careers")
    assert not urls.looks_like_company_jobs_url("https://remotive.com/jobs")

    class Client:
        def get_text(self, url):
            return 200, url, '<a href="https://boards.greenhouse.io/acme/jobs/1">r</a><meta content="https://acme.example/jobs">'

    links = urls.fetch_page_links("https://acme.example/careers", 1, client=Client())
    assert "https://boards.greenhouse.io/acme/jobs/1" in links and "https://acme.example/jobs" in links


def test_arbeitnow_null_next_link_stops_pagination():
    c = FakeClient({"data": [{"title": "ML Engineer", "url": "https://a/jobs/1"}], "links": {"next": None}})
    rows, discards = collectors.collect_arbeitnow_urls(1, 3, api_url="https://a/api", request_json=c.request_json)
    assert len(rows) == 1 and discards == [] and c.requested == ["https://a/api"]
