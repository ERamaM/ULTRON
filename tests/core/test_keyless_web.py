import sys
from threading import Barrier
from types import ModuleType
from urllib.parse import parse_qs, urlsplit

import pytest

from ultron.core.web_search import cascade, ddg_html, ddgs_client, searxng
from ultron.core.web_search.dedup import deduplicate_hits
from ultron.core.web_search.models import ProviderUnavailable, SearchHit
from ultron.core.web_search.query_builder import build_queries, relaxed_queries
from ultron.infra import searxng as searxng_api
from ultron.infra.net import HttpError


def hit(url):
    return SearchHit(" Nurse ", url, "", "nurse", "test")


def test_queries_follow_profile():
    profile = {"criteria": {
        "target_titles": ["Nurse Practitioner", "Clinical Nurse", "Paediatric Nurse"],
        "location_scope": {"preferred": ["Queensland", "Brisbane", "Australia"]},
        "work_mode": "onsite",
    }, "query_templates": {"site_scopes": ["site:careers.example.com"]}}
    queries = build_queries(profile, 10)
    assert len(queries) == 10
    assert sum(query.startswith("site:") for query in queries) == 6
    assert all("(jobs OR careers OR apply OR empleo)" in query for query in queries if not query.startswith("site:"))
    assert any(query.startswith("site:myworkdayjobs.com ") for query in queries)
    assert all("onsite" in query and "Remote" not in query for query in queries)
    assert all(any(region in query for region in ("Queensland", "Brisbane", "Australia")) for query in queries)
    assert build_queries({}, 3, query=" clinic manager ")[0].startswith("clinic manager (jobs OR careers")
    assert build_queries({}, 5) == []
    assert build_queries(profile, -1) == []
    assert relaxed_queries('site:boards.greenhouse.io "Nurse Practitioner"') == (
        "site:boards.greenhouse.io Nurse Practitioner", "site:job-boards.greenhouse.io Nurse Practitioner",
    )
    assert relaxed_queries("nurse") == ()
    profile["criteria"]["remote_policy"] = {"required": "full_remote"}
    assert all("remote" in item and "onsite" not in item for item in build_queries(profile, 8))


def test_finance_plan_gives_workday_two_role_queries():
    profile = {"criteria": {"target_titles": ["Financial Analyst", "Analista Financiero", "FP&A Analyst"],
                            "location_scope": {"preferred": ["Spain", "Madrid"]}}}
    plan = build_queries(profile, 20)
    workday = [query for query in plan if query.startswith("site:myworkdayjobs.com ")]
    assert workday == ["site:myworkdayjobs.com Analista Financiero Spain", "site:myworkdayjobs.com FP&A Analyst Spain"]


def test_role_override_keeps_profile_scope():
    profile = {"criteria": {"target_titles": ["Teacher"], "work_mode": "onsite",
                            "location_scope": {"preferred": ["Ireland"]}}}
    broad, scoped = build_queries(profile, 2, query="clinical nurse")
    assert broad == "clinical nurse (jobs OR careers OR apply OR empleo) Ireland onsite"
    assert scoped.startswith("site:")
    assert "clinical nurse Ireland onsite" in scoped and "Teacher" not in scoped


def test_unusable_searxng_rows_still_allow_alternate_host(monkeypatch):
    calls = []

    def read(_base, path, **_options):
        query = parse_qs(urlsplit(path).query)["q"][0]
        calls.append(query)
        if query.startswith("site:job-boards.greenhouse.io "):
            return {"results": [{"url": "https://example.org/jobs/1", "title": "Nurse"}]}
        return {"results": [{"title": "A row with no link"}]}

    monkeypatch.setenv("ULTRON_SEARXNG_URL", "http://localhost:8888")
    monkeypatch.setattr(searxng, "read_json", read)
    result = cascade.search_web(['site:boards.greenhouse.io "nurse"'], "searxng")
    assert [row.url for row in result.hits] == ["https://example.org/jobs/1"]
    assert calls == ['site:boards.greenhouse.io "nurse"', "site:boards.greenhouse.io nurse",
                     "site:job-boards.greenhouse.io nurse"]


@pytest.mark.parametrize("healthy", [False, True])
def test_searxng_decoder_recursion_allows_auto_fallback(monkeypatch, healthy):
    monkeypatch.setenv("ULTRON_SEARXNG_URL", "http://localhost:8888")
    monkeypatch.setattr(searxng_api, "read_loopback", lambda *a, **k: (200, b'{"results":[]}'))
    if healthy:
        monkeypatch.setattr(cascade, "json_health", lambda **_: True)
    monkeypatch.setattr(searxng_api, "json", type("Decoder", (), {"loads": staticmethod(_decoder_recursion)}))
    monkeypatch.setattr(ddgs_client, "available", lambda: False)
    monkeypatch.setattr(ddg_html, "search", lambda queries: ([hit("https://example.org/jobs/1")], len(queries)))
    assert cascade.search_web(["nurse"], "auto").provider_used == "ddg_html"


def _decoder_recursion(_payload):
    raise RecursionError("synthetic decoder failure")


def test_provider_urls_are_normalized():
    result = deduplicate_hits([hit(url) for url in (
        "https://EXAMPLE.org/jobs/1?req=2&utm_source=ad#fragment",
        "https://example.org/jobs/1?req=2",
        "https://example.org/jobs/1?req=3",
        "https://alice@example.org/jobs/1",
        "http://127.0.0.1/jobs/1",
        "bad", "javascript:alert(1)",
    )])
    assert [item.url for item in result] == ["https://example.org/jobs/1?req=2", "https://example.org/jobs/1?req=3"]


def test_searxng_parallel_relaxation_and_pages(monkeypatch):
    barrier = Barrier(2)

    def read(base, path, *, timeout):
        query = parse_qs(urlsplit(path).query)
        q, page = query["q"][0], query["pageno"][0]
        if '"' in q:
            barrier.wait(timeout=5)
            return {"results": []}
        if q.split()[0] == "site:boards.greenhouse.io":
            return {"results": []}
        assert base == "http://localhost:8888"
        return {"results": [{"title": " A   title ", "url": f"https://example.org/job/{page}", "content": " Facts "}]}

    monkeypatch.setenv("ULTRON_SEARXNG_URL", "http://localhost:8888")
    monkeypatch.setattr(searxng, "read_json", read)
    results, count = searxng.search(['site:boards.greenhouse.io "nurse"', '"teacher"'], max_pages=8)
    assert count == 2
    assert [result.title for result in results] == ["A title"] * 4
    assert results[0].query == "site:job-boards.greenhouse.io nurse"
    assert results[1].url.endswith("/2")


def test_searxng_retries_transient_errors(monkeypatch):
    calls = []

    def read(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise HttpError("busy", status_code=503)
        return {"results": "malformed"}

    monkeypatch.delenv("ULTRON_SEARXNG_URL", raising=False)
    monkeypatch.setattr(searxng, "read_json", read)
    monkeypatch.setattr(searxng.time, "sleep", lambda _: None)
    assert searxng.search(["teacher"]) == ([], 1)
    assert len(calls) == 2
    monkeypatch.setenv("ULTRON_SEARXNG_PORT", "abc")
    with pytest.raises(ValueError):
        searxng.search(["teacher"])


def test_html_and_lite_fallback(monkeypatch):
    html = '''
    <div class="result result--ad"><a class="result__a" href="https://ads.example.org">ad</a></div>
    <div class="result"><a class="result__a" href="/y.js">ad</a></div>
    <div class="result"><a class="result__a" href="/l/?uddg=https%3A%2F%2Fjobs.example.org%2F1">Nurse</a>
    <span class="result__snippet">Job facts</span></div>
    '''
    lite = '''
    <table><tr><td><a class="result-link" href="https://jobs.example.org/2">Teacher</a></td></tr>
    <tr><td class="result-snippet">Role facts</td></tr></table>
    '''
    assert [(row.title, row.snippet, row.url) for row in
            ddg_html.parse_results(html, "https://html.duckduckgo.com/html/", "nurse")] == [
        ("Nurse", "Job facts", "https://jobs.example.org/1"),
    ]
    calls = []

    class Http:
        def __init__(self, **options):
            assert options["min_interval_per_host"] >= 1.5

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get_text(self, url, *, allowed_domains):
            assert allowed_domains == ("html.duckduckgo.com", "lite.duckduckgo.com")
            calls.append(url)
            if "html.duckduckgo" in url:
                raise HttpError("unavailable")
            return 200, url, lite

    monkeypatch.setattr(ddg_html, "HttpClient", Http)
    results, count = ddg_html.search(["teacher"] * 11)
    assert len(calls) == 20
    assert count == len(results) == 10
    assert results[0].snippet == "Role facts"


def test_optional_ddgs(monkeypatch):
    monkeypatch.setitem(sys.modules, "ddgs", None)
    assert not ddgs_client.available()
    with pytest.raises(ProviderUnavailable):
        ddgs_client.search(["nurse"])

    module = ModuleType("ddgs")
    exceptions = ModuleType("ddgs.exceptions")

    class Failure(Exception):
        pass

    class DDGS:
        def __init__(self, *, timeout):
            assert timeout <= 5

        def text(self, query, *, max_results):
            if query == "fail":
                raise Failure
            return [{"title": "Nurse", "href": "https://jobs.example.org/1", "body": "Job facts"}]

    module.DDGS = DDGS
    exceptions.DDGSException = Failure
    monkeypatch.setitem(sys.modules, "ddgs", module)
    monkeypatch.setitem(sys.modules, "ddgs.exceptions", exceptions)
    assert ddgs_client.available()
    results, count = ddgs_client.search(["fail", "nurse"])
    assert count == 2
    assert results[0].provider == "ddg"


def test_cascade_and_multi(monkeypatch):
    calls = []

    def local(queries):
        calls.append("local")
        return [hit("https://example.org/1?utm_source=searxng")], len(queries)

    def failed(queries):
        calls.append("ddgs")
        raise ProviderUnavailable

    def html(queries):
        calls.append("html")
        return [hit("https://example.org/1")], len(queries)

    monkeypatch.setattr(cascade, "json_health", lambda **_: False)
    monkeypatch.setattr(ddgs_client, "available", lambda: True)
    monkeypatch.setattr(searxng, "search", local)
    monkeypatch.setattr(ddgs_client, "search", failed)
    monkeypatch.setattr(ddg_html, "search", html)
    assert cascade.search_web(["nurse"], "auto").provider_used == "ddg_html"
    assert calls == ["ddgs", "html"]
    multi = cascade.search_web(["nurse"], "multi")
    assert (multi.raw_hits, len(multi.hits), multi.queries_run) == (2, 1, 2)
    assert not multi.metadata()["fallback_to_agent_web_search"]
    monkeypatch.setattr(ddg_html, "search", failed)
    assert cascade.search_web(["nurse"], "auto").metadata()["fallback_to_agent_web_search"]
    monkeypatch.setenv("ULTRON_WEB_SEARCH_PROVIDER", "none")
    assert not cascade.search_web(["nurse"]).metadata()["fallback_to_agent_web_search"]
    with pytest.raises(ValueError):
        cascade.search_web(["nurse"], "unknown")


def test_provider_deadlines_stop_queued_queries(monkeypatch):
    monkeypatch.setattr(searxng, "read_json", lambda *_a, **_kw: pytest.fail("deadline was ignored"))
    assert searxng.search(["nurse"], deadline=0) == ([], 0)
    assert ddg_html.search(["nurse"], deadline=0) == ([], 0)
    assert cascade.search_web(["nurse"], "searxng", deadline=0).metadata()["fallback_to_agent_web_search"]


def test_searxng_timeout_leaves_no_time_for_retry_sleep(monkeypatch):
    now = [0.0]

    def read(*_args, **_kwargs):
        now[0] = 1.0
        raise HttpError("request timed out")

    monkeypatch.setenv("ULTRON_SEARXNG_URL", "http://localhost:8888")
    monkeypatch.setattr(searxng, "read_json", read)
    monkeypatch.setattr(searxng, "time", type("Clock", (), {
        "monotonic": lambda: now[0],
        "sleep": lambda _: pytest.fail("retry sleep would run past the deadline"),
    }))
    assert searxng.search(["nurse"], deadline=1.0) == ([], 1)

def test_searxng_runs_initial_queries_before_relaxing(monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    calls = []
    queries = [f'"position {i}"' for i in range(12)]

    def read(_base, path, **_options):
        q = parse_qs(urlsplit(path).query)["q"][0]
        calls.append(q)
        return {"results": []}

    monkeypatch.setattr(searxng, "read_json", read)
    assert searxng.search(queries) == ([], len(queries))
    assert set(calls[:len(queries)]) == set(queries)
    assert len(calls) == 2 * len(queries)
