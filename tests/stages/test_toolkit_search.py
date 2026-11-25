"""``ultron search`` (role job APIs, watched boards, the watch's pending postings) and the day-2 watch."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from toolkit_helpers import GH_BOARD, GH_PAGE, OTHER_POSTING, POSTING

from ultron import seeds
from ultron.cli import main
from ultron.core.feeds import role_search
from ultron.profiles import store as profiles
from ultron.stages import http_factory, role_feeds
from ultron.stages.watch import latest_shortlist
from ultron.toolkit import search, shortlist

SLUG = profiles.EXAMPLE_SLUG
REMOTIVE = {"jobs": [
    {"title": "Senior Machine Learning Engineer", "company_name": "Acme", "url": "https://remotive.com/remote-jobs/1",
     "candidate_required_location": "Europe", "publication_date": "2026-11-20T10:00:00"},
    {"title": "Office Manager", "company_name": "Remco", "url": "https://remotive.com/remote-jobs/2",
     "candidate_required_location": "Europe"},
    {"title": "Senior Frontend Engineer", "company_name": "Remco", "url": "https://remotive.com/remote-jobs/3",
     "candidate_required_location": "Europe"},
]}
HIMALAYAS = {"jobs": [{"title": "ML Engineer", "companyName": "Hico", "applicationLink": "https://hico.example/jobs/9",
                       "locationRestrictions": [], "pubDate": 1795000000}]}
JOBICY = {"jobs": [{"jobTitle": "Machine Learning Engineer", "companyName": "Acme", "url": "https://jobicy.com/jobs/7",
                    "jobGeo": "EMEA", "pubDate": "2026-11-21"}]}
ADZUNA = {"results": [{"title": "Senior Machine Learning Engineer", "company": {"display_name": "Adco"},
                       "location": {"display_name": "Madrid"}, "redirect_url": "https://www.adzuna.es/details/5",
                       "created": "2026-11-22T00:00:00Z"}]}
REED = {"results": [{"jobId": 77, "jobTitle": "Machine Learning Engineer", "employerName": "Reedco",
                     "locationName": "Remote", "jobUrl": "https://www.reed.co.uk/jobs/77"}]}
SEEN: list[httpx.Request] = []


def handler(request: httpx.Request) -> httpx.Response:
    SEEN.append(request)
    host, path = request.url.host, request.url.path
    if host == "remotive.com" and path.startswith("/api"):
        return httpx.Response(200, json=REMOTIVE)
    if host == "remotive.com":  # a listing page links to the employer's posting
        return httpx.Response(200, text=f'<html><body><a href="{POSTING}">Apply</a></body></html>')
    if host == "jobicy.com" and path.startswith("/jobs/"):
        return httpx.Response(200, text=f'<html><body><a href="{OTHER_POSTING}">Apply</a></body></html>')
    if host == "himalayas.app" and path == "/jobs/api/search":
        return httpx.Response(200, json=HIMALAYAS)
    if host == "jobicy.com" and path == "/api/jobs":
        return httpx.Response(200, json=JOBICY)
    if host == "api.adzuna.com":
        return httpx.Response(200, json=ADZUNA)
    if host == "www.reed.co.uk":
        return httpx.Response(200, json=REED)
    if "greenhouse" in host and path.startswith("/v1/boards/acme"):
        return httpx.Response(200, content=GH_BOARD)
    if host == "boards.greenhouse.io":
        return httpx.Response(200, text=GH_PAGE)
    return httpx.Response(404)


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)
    for name in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY", "ADZUNA_COUNTRY", "REED_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    SEEN.clear()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    profiles.install_example()


def test_search_queries_keyless_role_apis_and_ranks_by_title():
    out = search.search_jobs(SLUG)
    assert out["queries"] and out["sources"]["remotive"]["status"] == "ok"
    assert out["sources"]["adzuna"] == {"status": "not_configured"} and out["sources"]["reed"]["status"] == "not_configured"
    titles = [d["title"] for d in out["dossiers"]]
    assert "Office Manager" not in titles  # does not match the profile's role family
    assert {d["source"] for d in out["dossiers"]} >= {"Remotive", "Himalayas", "Jobicy"}
    assert all(d["status"] == "listed" and d["memory"] == "new" for d in out["dossiers"])
    assert search.relevance(titles[0], out["queries"]) >= search.relevance(titles[-1], out["queries"])


def test_search_caches_each_source():
    search.search_jobs(SLUG)
    first = sum(1 for r in SEEN if r.url.host == "remotive.com")
    out = search.search_jobs(SLUG)
    assert sum(1 for r in SEEN if r.url.host == "remotive.com") == first
    assert out["sources"]["remotive"]["status"] == "cached"


def test_keyed_sources_send_their_keys_only_to_their_hosts(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "id-1")
    monkeypatch.setenv("ADZUNA_APP_KEY", "secret-key")
    monkeypatch.setenv("ADZUNA_COUNTRY", "es")
    monkeypatch.setenv("REED_API_KEY", "reed-key")
    out = search.search_jobs(SLUG, "machine learning engineer")
    assert out["sources"]["adzuna"]["status"] == "ok" and out["sources"]["reed"]["status"] == "ok"
    reed = next(r for r in SEEN if r.url.host == "www.reed.co.uk")
    assert reed.headers["authorization"] == "Basic " + base64.b64encode(b"reed-key:").decode()
    assert all("authorization" not in r.headers for r in SEEN if r.url.host != "www.reed.co.uk")
    assert "secret-key" not in json.dumps(out)
    assert {"Adzuna", "Reed"} <= {d["source"] for d in out["dossiers"]}


def test_failed_sources_are_reported_without_stopping(monkeypatch):
    def broken(request):
        return httpx.Response(500) if request.url.host == "remotive.com" else handler(request)
    http_factory.set_test_overrides(transport=httpx.MockTransport(broken), resolver=lambda h, p: ["93.184.216.34"],
                                    sleep=lambda s: None, min_interval_per_host=0.0)
    out = search.search_jobs(SLUG)
    assert out["sources"]["remotive"]["status"] == "failed" and out["sources"]["jobicy"]["status"] == "ok"


def test_parsers_skip_rows_without_a_link():
    assert role_search._jobicy_rows({"jobs": [{"jobTitle": "x", "url": "ftp://bad"}]}) == []
    assert role_search._remotive_rows({"jobs": "not a list"}) == []
    assert role_search._reed_rows({"results": [{"jobId": 5, "jobTitle": "ML"}]})[0]["url"].endswith("/5")
    assert role_search._himalayas_rows(HIMALAYAS)[0]["posted"].startswith("20")


def test_query_terms_and_relevance():
    profile = {"criteria": {"target_titles": ["A", "B", "C", "D"], "role_focus_terms": ["x"]}}
    assert role_feeds.query_terms(profile) == ["A", "B", "C"]
    assert role_feeds.query_terms({"criteria": {"role_focus_terms": ["ml"]}}) == ["ml"]
    assert role_feeds.query_terms(profile, " custom ") == ["custom"]
    assert role_feeds.relevance("Senior ML Engineer", ["ml engineer"]) == 1.0
    assert role_feeds.relevance("Chef", []) == 0.0


def test_watch_records_new_postings_from_role_apis_and_search_serves_them_once(capsys):
    assert main(["watch", "--profile", SLUG]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["new_postings"] >= 1 and out["sources"]["remotive"]["status"] == "ok" and out["digest"]
    served = search.search_jobs(SLUG)
    watch_rows = [d for d in served["dossiers"] if d["source"] == "watch"]
    assert watch_rows and served["dossiers"][0]["source"] == "watch"
    again = search.search_jobs(SLUG)
    assert not [d for d in again["dossiers"] if d["source"] == "watch"]  # queued: served once


def test_watch_reports_shortlisted_postings_that_closed(capsys):
    gone = "https://careers.example.org/jobs/gone"
    shortlist.save_shortlist(SLUG, [
        {"url": POSTING, "verdict": "keep", "title": "Senior Data Scientist", "company": "Acme"},
        {"url": gone, "verdict": "keep", "title": "ML Lead", "company": "Example Org"},
    ])
    assert len(latest_shortlist(SLUG)) == 2
    assert main(["watch", "--profile", SLUG]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [c["apply_url"] for c in out["closed"]] == [gone]
    assert "Closed since the last shortlist" in Path(out["digest"]).read_text(encoding="utf-8")


def test_cli_search(capsys):
    assert main(["search", "--profile", SLUG, "--query", "machine learning", "--new-only", "--limit", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["queries"] == ["machine learning"] and len(out["dossiers"]) <= 2
    assert main(["search", "--profile", "missing-profile"]) != 0


def test_search_results_can_be_saved_by_id(capsys):
    assert main(["watchlist", "add", "--profile", SLUG, "--board", "greenhouse:acme"]) == 0
    capsys.readouterr()
    found = search.search_jobs(SLUG)
    board_row = next(d for d in found["dossiers"] if d["source"] == "greenhouse:acme")
    api_row = next(d for d in found["dossiers"] if d["source"] == "Himalayas")  # an employer link, not an aggregator
    out = shortlist.save_shortlist(SLUG, [{"id": board_row["id"], "verdict": "keep"},
                                          {"id": api_row["id"], "verdict": "discard"}])
    assert out["saved"] == {"keep": 1, "unsure": 0, "discard": 1} and not out["not_saved"]
    again = {d["id"]: d["memory"] for d in search.search_jobs(SLUG)["dossiers"]}
    assert again[board_row["id"]] == "seen" and again[api_row["id"]] == "rejected"


def test_an_explicit_query_matches_its_own_role():
    default = [d["title"] for d in search.search_jobs(SLUG)["dossiers"]]
    asked = [d["title"] for d in search.search_jobs(SLUG, "frontend engineer")["dossiers"]]
    assert "Senior Frontend Engineer" not in default and "Senior Frontend Engineer" in asked


def test_only_returned_watch_postings_are_marked_served(capsys):
    assert main(["watch", "--profile", SLUG]) == 0
    pending = json.loads(capsys.readouterr().out)["new_postings"]
    assert pending >= 2
    first = search.search_jobs(SLUG, limit=1)["dossiers"]
    assert [d["source"] for d in first] == ["watch"]
    rest = [d for d in search.search_jobs(SLUG)["dossiers"] if d["source"] == "watch"]
    assert len(rest) == pending - 1


def test_watch_board_postings_keep_their_board(capsys):
    assert main(["watchlist", "add", "--profile", SLUG, "--board", "greenhouse:acme"]) == 0
    assert main(["watch", "--profile", SLUG]) == 0
    capsys.readouterr()
    served = [d for d in search.search_jobs(SLUG)["dossiers"] if d.get("board") == "greenhouse:acme"]
    assert served and served[0]["source"] == "watch"
    from ultron.toolkit.inspect import cached_postings
    assert cached_postings(SLUG)[served[0]["id"]]["source_key"] == "greenhouse:acme"


def test_a_failing_local_feed_is_reported_and_not_cached(monkeypatch):
    calls = []

    def failing(client, source, timeout):
        calls.append(1)
        return [], [{"url": source["url"], "reason": "boom"}]
    monkeypatch.setitem(role_feeds.COLLECTORS, "arbeitnow", failing)
    assert search.search_jobs(SLUG)["sources"]["arbeitnow"]["status"] == "failed"
    assert search.search_jobs(SLUG)["sources"]["arbeitnow"]["status"] == "failed" and len(calls) == 2


def test_watch_records_the_employer_posting_behind_an_aggregator(capsys):
    assert main(["watch", "--profile", SLUG]) == 0
    capsys.readouterr()
    served = {d["url"] for d in search.search_jobs(SLUG)["dossiers"] if d["source"] == "watch"}
    assert POSTING in served and not {urlsplit(url).hostname for url in served} & {"remotive.com", "jobicy.com"}


def test_pending_postings_follow_an_explicit_query(capsys):
    assert main(["watch", "--profile", SLUG]) == 0
    capsys.readouterr()
    asked = search.search_jobs(SLUG, "frontend engineer")["dossiers"]
    assert not [d for d in asked if d["source"] == "watch"]  # no pending posting is a frontend role
    assert [d for d in search.search_jobs(SLUG)["dossiers"] if d["source"] == "watch"]  # still pending


def test_keyed_sources_are_aggregators():
    from ultron.core.feeds.urls import is_aggregator_host
    assert is_aggregator_host("www.adzuna.es") and is_aggregator_host("www.reed.co.uk")


def test_pending_postings_keep_their_remote_hint():
    from contextlib import closing

    from ultron.storage import ledger, watchlist
    candidate = {"identity_key": "greenhouse:acme:999", "normalized_url": "https://boards.greenhouse.io/acme/jobs/999",
                 "ats_title": "Senior Machine Learning Engineer", "ats_company": "Acme",
                 "ats_location": "United States", "ats_remote_hint": "Remote - Europe"}
    with closing(ledger.connect()) as conn:
        watchlist.add_pending(conn, SLUG, [(candidate["identity_key"], candidate)])
    served = [d["id"] for d in search.search_jobs(SLUG)["dossiers"] if d["source"] == "watch"]
    assert "greenhouse:acme:999" in served


def test_a_failing_term_keeps_the_other_terms_rows(monkeypatch):
    def flaky(request):
        if request.url.host == "remotive.com" and "Second" in str(request.url):
            return httpx.Response(500)
        return handler(request)
    http_factory.set_test_overrides(transport=httpx.MockTransport(flaky), resolver=lambda h, p: ["93.184.216.34"],
                                    sleep=lambda s: None, min_interval_per_host=0.0)
    from ultron.core.feeds.role_search import SOURCES
    remotive = next(s for s in SOURCES if s.name == "remotive")
    status, rows = role_feeds.role_source_rows(remotive, ["Machine Learning", "Second Role"],
                                               http_factory.client_for_run({"settings": {
                                                   "max_pages": 50, "max_seeds_per_run": 10,
                                                   "max_requests_per_host": 50, "ats_api_timeout": 5}}), 5)
    assert status["status"] == "partial" and rows


def test_aggregator_resolution_moves_past_known_listings(monkeypatch, capsys):
    from ultron.stages import watch as watch_stage
    monkeypatch.setattr(watch_stage, "MAX_RESOLVED", 1)
    assert main(["watch", "--profile", SLUG]) == 0
    capsys.readouterr()
    first = set(role_feeds.known_resolutions())
    assert main(["watch", "--profile", SLUG]) == 0
    capsys.readouterr()
    assert len(role_feeds.known_resolutions()) > len(first)  # the second watch followed a new listing


def test_search_flags_an_aggregator_listing_with_its_employer_postings_memory():
    from ultron.toolkit import inspect
    listing = next(d for d in search.search_jobs(SLUG)["dossiers"] if d["source"] == "Remotive")
    (read,) = inspect.inspect_jobs(SLUG, [listing["url"]])["dossiers"]
    shortlist.save_shortlist(SLUG, [{"id": read["id"], "verdict": "discard"}])
    again = {d["id"]: d["memory"] for d in search.search_jobs(SLUG)["dossiers"]}
    assert again[listing["id"]] == "rejected"
