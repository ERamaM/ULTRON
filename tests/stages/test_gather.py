import json
from contextlib import closing
from threading import Barrier
from urllib.parse import urlsplit

import httpx
import pytest
from toolkit_helpers import (
    EMPLOYER,
    EMPLOYER_PAGE,
    GH_PAGE,
    POSTING,
    offline,
)
from toolkit_helpers import (
    handler as default_handler,
)

from ultron.cli import main
from ultron.core.web_search.models import SearchHit, WebSearchResult
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.storage import ledger, scout_memory, watchlist
from ultron.toolkit import aggregators, gather
from ultron.toolkit.dossier import GEO_EXCERPT, ROLE_EXCERPT, posting_dossier
from ultron.toolkit.inspect import cache_postings, cached_postings
from ultron.toolkit.mcp_server import call_tool
from ultron.toolkit.shortlist import save_shortlist

SLUG = profiles.EXAMPLE_SLUG
DEAD = "https://himalayas.app/companies/acme/jobs/senior-machine-learning-engineer"
LOOKUP = "https://remoterocketship.com/company/extra/jobs/senior-ml-engineer"
LOOKUP_TARGET = "https://boards.greenhouse.io/extra/jobs/188"
NO_COMPANY = "https://jobgether.com/jobs/unknown"
FEED = "https://hico.example/jobs/9"
WATCH = "https://watch.example/jobs/21"
REJECTED = "https://reject.example/jobs/999"
APPLIED = "https://applied.example/jobs/23"
SOCIAL = "https://www.linkedin.com/in/nurse"


def hit(url, title, *, query="initial"):
    return SearchHit(title, url, "", query, "searxng")


@pytest.fixture(autouse=True)
def _profile(tmp_path, monkeypatch):
    offline(tmp_path, monkeypatch)


def _setup_transport(monkeypatch, handler):
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda _h, _p: ["93.184.216.34"],
        sleep=lambda _s: None, min_interval_per_host=0.0,
    )
    for name in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "REED_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_gather_reads_sources_reuses_board_posts_and_caches_for_save(monkeypatch):
    barrier = Barrier(2)
    requests = []
    resolution_queries = []
    feed_calls = []

    def handler(request):
        requests.append(str(request.url))
        host, path = request.url.host, request.url.path
        if host == "himalayas.app" and path == "/jobs/api/search":
            if not feed_calls:
                barrier.wait(timeout=15)
            feed_calls.append(path)
            return httpx.Response(200, json={"jobs": [{
                "title": "Senior Machine Learning Engineer", "companyName": "Hico", "applicationLink": FEED,
                "locationRestrictions": [],
            }]})
        if host in {"himalayas.app", "remoterocketship.com", "jobgether.com"}:
            return httpx.Response(200, text="<html><body>No links here</body></html>")
        if str(request.url) in {WATCH, FEED}:
            return httpx.Response(200, text=EMPLOYER_PAGE.replace("Example Org", "Watched" if host == "watch.example" else "Hico"))
        if str(request.url) == LOOKUP_TARGET:
            return httpx.Response(200, text=GH_PAGE.replace("Acme", "Extra"))
        return default_handler(request)

    def web(queries, provider, *, deadline):
        assert provider == "auto" and len(queries) == 5
        barrier.wait(timeout=15)
        hits = (
            hit(POSTING, "Senior Machine Learning Engineer at Acme"),
            hit(DEAD, "Senior Machine Learning Engineer at Acme"),
            hit(LOOKUP, "Senior ML Engineer at Extra"),
            hit(NO_COMPANY, "Senior ML Engineer"),
            hit(REJECTED, "Senior ML Engineer"), hit(APPLIED, "Senior ML Engineer"),
            hit(SOCIAL, "Senior ML Engineer"), hit("https://example.org", "Root"),
        )
        return WebSearchResult("ok", "searxng", len(hits), len(queries), hits)

    def lookup(queries, *, deadline):
        resolution_queries.extend(queries)
        return [hit(LOOKUP_TARGET, "Senior ML Engineer", query=query)
                for query in queries if "extra" in query], len(queries)

    _setup_transport(monkeypatch, handler)
    monkeypatch.setattr(gather, "search_web", web)
    monkeypatch.setattr(gather.searxng, "search", lookup)
    with closing(ledger.connect()) as conn:
        scout_memory.record_verdicts(conn, SLUG, "previous", [
            {"identity_key": gather._identity(REJECTED), "verdict": "discard"}])
        conn.execute("INSERT INTO jobs (identity_key, canonical_url, first_seen, last_seen, review_state)"
                     " VALUES (?, ?, '2026-11-25', '2026-11-25', 'applied')", (gather._identity(APPLIED), APPLIED))
        conn.commit()
        watchlist.add_pending(conn, SLUG, [(gather._identity(WATCH), {
            "normalized_url": WATCH, "ats_title": "Senior Machine Learning Engineer",
            "ats_company": "Watched", "ats_location": "Spain", "ats_remote_hint": "Remote",
        })])

    result = gather.gather_candidates(SLUG, max_queries=5, web_provider="auto", detail=True)
    assert result["web_search"]["provider"] == "searxng" and result["web_search"]["unique_urls"] == 8
    assert result["sources"]["watch"]["results"] == 1 and result["sources"]["himalayas"]["results"] >= 1
    assert result["inspected"]["skipped_memory"] == 2
    assert not {REJECTED, APPLIED, SOCIAL}.intersection(requests)
    assert any("extra" in query for query in resolution_queries)
    assert any(row["url"] == NO_COMPANY for row in result["unresolved"])
    assert all("hint" not in row and "snippet" not in row for row in result["dossiers"])
    assert all(urlsplit(row["url"]).hostname not in {"himalayas.app", "remoterocketship.com", "jobgether.com"}
               for row in result["dossiers"])
    by_url = {row["url"]: row for row in result["dossiers"]}
    assert by_url[LOOKUP_TARGET]["via"] == "targeted_ats_search"
    assert LOOKUP not in {row["url"] for row in result["unresolved"]}
    assert by_url[WATCH]["source"] == "watch"
    assert by_url[FEED]["status"] == "open"
    with closing(ledger.connect()) as conn:
        assert not watchlist.pending(conn, SLUG)
    cache = cached_postings(SLUG)
    assert by_url[WATCH]["id"] in cache
    assert cache[by_url[WATCH]["id"]]["job"]["body_text"]
    saved = save_shortlist(SLUG, [{"id": by_url[WATCH]["id"], "verdict": "keep", "priority": 1}])
    assert saved["saved"]["keep"] == 1


def test_gather_expands_the_requested_ats_board(monkeypatch):
    web = WebSearchResult("ok", "ddg_html", 1, 1, (hit(POSTING, "ML Engineer at Acme"),))
    monkeypatch.setattr(gather, "search_web", lambda *_, **kwargs: web)
    monkeypatch.setattr(gather, "discover_sources", lambda *a, **k: gather_empty_sources())
    result = gather.gather_candidates(SLUG)
    assert any(row.get("via") == "same_board" and row["id"] == "greenhouse:acme:4012346"
               for row in result["dossiers"])


def test_cli_and_mcp_gather(monkeypatch, capsys):
    monkeypatch.setenv("ULTRON_WEB_SEARCH_PROVIDER", "none")
    monkeypatch.setattr(gather, "discover_sources", lambda *args, **kwargs: gather_empty_sources())
    code = main(["gather", "--profile", SLUG, "--query", "Nurse", "--max-queries", "2", "--new-only"])
    cli = json.loads(capsys.readouterr().out)
    assert code == 0 and cli["queries"][0] == "Nurse"
    assert cli["web_search"]["status"] == "disabled" and cli["inspected"]["attempted"] == 0
    result = call_tool("gather_candidates", {"profile": SLUG, "web_provider": "none"})
    assert not result["isError"]
    assert json.loads(result["content"][0]["text"])["returned"] == 0
    for bad in ({"limit": True}, {"limit": 101}, {"max_queries": 0}, {"query": 3}, {"detail": "true"},
                {"web_provider": "invalid"}, {"path": "input.jsonl"}):
        assert call_tool("gather_candidates", {"profile": SLUG, **bad})["isError"]
    assert main(["gather", "--profile", SLUG, "--limit", "0"]) != 0
    assert main(["gather", "--profile", "invalid-slug-that-does-not-exist"]) != 0


def gather_empty_sources():
    from ultron.toolkit.search import SourceDiscovery

    return SourceDiscovery([], [], {}, set())


def test_gather_skips_seen_and_keeps_listed_when_read_fails(monkeypatch):
    seen = gather._identity(EMPLOYER)
    fresh = "https://no-network.example/jobs/a"
    with closing(ledger.connect()) as conn:
        scout_memory.record_verdicts(conn, SLUG, "previous", [{"identity_key": seen, "verdict": "keep"}])
    captured = []

    def record(request):
        captured.append(str(request.url))
        return httpx.Response(404)

    _setup_transport(monkeypatch, record)
    source = gather_empty_sources()
    for url in (EMPLOYER, fresh):
        source.dossiers.append({"id": gather._identity(url), "url": url, "source": "feed", "status": "listed",
                                "title": "ML Engineer", "company": "Sample", "location": "Spain"})
    monkeypatch.setattr(gather, "discover_sources", lambda *a, **k: source)
    result = gather.gather_candidates(SLUG, new_only=True, web_provider="none")
    assert EMPLOYER not in captured and result["inspected"]["skipped_memory"] == 1
    # A definitive 404 is closed and cannot reappear as a listed fallback.
    assert result["dossiers"] == []


def test_aggregator_clues_and_factual_excerpts():
    from ultron.core.models import ExtractedJob

    assert aggregators.aggregator_company(DEAD) == "Acme"
    assert aggregators.aggregator_company("https://builtin.com/company/example-org/jobs/engineer") == "Example Org"
    assert aggregators.aggregator_company("https://workingnomads.com/jobs/ambiguous") == ""
    assert aggregators.title_company("Senior Engineer @ Acme | jobs") == "Acme"
    assert aggregators.title_company("Acme - Senior Engineer") == "Acme"
    assert aggregators.matches_lookup(POSTING, "Senior ML Engineer", "Acme", "Senior ML Engineer")
    assert not aggregators.matches_lookup(POSTING, "Senior ML Engineer", "Other", "Senior ML Engineer")
    assert not aggregators.matches_lookup(POSTING, "Senior Software Engineer", "Acme", "Senior Machine Learning Engineer")
    assert aggregators.matches_lookup(POSTING, "Senior ML Engineer", "Acme", "Senior Machine Learning Engineer at Acme")
    assert not aggregators.matches_lookup(DEAD, "Senior ML Engineer", "Acme", "Senior ML Engineer")
    assert "scope:outside" not in aggregators.lookup_query('Acme" scope:outside', "Engineering")
    job = ExtractedJob(apply_url=EMPLOYER, domain="careers.example.org", title="ML Engineer", company="Sample",
                       body_text="EMEA Remote, hybrid in Madrid. " + "Long role description. " * 140)
    dossier = posting_dossier(job, "job", "open")
    assert "hint" not in dossier
    assert "EMEA Remote" in dossier["geo_excerpt"] and "hybrid" in dossier["geo_excerpt"]
    assert GEO_EXCERPT == 350 and len(dossier["role_excerpt"]) == ROLE_EXCERPT == 700


def test_open_source_dossier_is_not_inspected_again(monkeypatch):
    candidate = {"id": gather._identity(POSTING), "url": POSTING, "source": "greenhouse:acme",
                 "status": "open", "title": "ML Engineer", "company": "Acme", "location": "Spain"}
    source = gather_empty_sources()
    source.dossiers.extend([candidate, {**candidate, "source": "watch", "status": "listed"}])
    web = WebSearchResult("ok", "ddg_html", 1, 1, (hit(POSTING, "ML Engineer at Acme"),))
    monkeypatch.setattr(gather, "discover_sources", lambda *a, **k: source)
    monkeypatch.setattr(gather, "search_web", lambda *a, **k: web)
    result = gather.gather_candidates(SLUG)
    assert result["inspected"]["attempted"] == 0
    (dossier,) = result["dossiers"]
    assert dossier["source"] == "watch" and dossier["status"] == "open"


def test_listed_fallback_reuses_canonical_ats_memory(monkeypatch):
    custom = "https://careers.example.org/o/senior-ml-engineer"
    cached = {"identity_key": "recruitee:exampleorg:senior-ml-engineer", "status": "listed", "url": custom,
              "source_type": "ats_api", "source_key": "recruitee:exampleorg", "job": {}}
    cache_postings(SLUG, [cached])
    source = gather_empty_sources()
    source.dossiers.append({"id": gather._identity(custom), "url": custom, "source": "feed", "status": "listed",
                            "title": "ML Engineer", "company": "Example Org", "location": "Spain"})
    monkeypatch.setattr(gather, "discover_sources", lambda *a, **k: source)
    with closing(ledger.connect()) as conn:
        scout_memory.record_verdicts(conn, SLUG, "previous", [
            {"identity_key": cached["identity_key"], "verdict": "discard"}])
    _setup_transport(monkeypatch, lambda _: pytest.fail("rejected ATS identity should not be requested"))
    result = gather.gather_candidates(SLUG, web_provider="none", new_only=True)
    assert result["returned"] == 0
    assert result["inspected"]["skipped_memory"] == 1 and result["inspected"]["attempted"] == 0
