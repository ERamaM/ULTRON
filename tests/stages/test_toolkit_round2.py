"""Toolkit round 2: parallel reads behind a host gate, board-first reads, compact replies and better search inputs."""

from __future__ import annotations

import html
import json
import threading
import time

import httpx
import pytest
from toolkit_helpers import EMPLOYER, POSTING, handler, offline

from ultron.infra.net import HostGate, VendorBudget
from ultron.profiles import store as profiles
from ultron.stages import http_factory, role_feeds, scout_resolve
from ultron.toolkit import dossier, inspect, shortlist

SLUG = profiles.EXAMPLE_SLUG
TEXT = ("Senior machine learning engineer building LLM systems in Python. Fully remote within the European Union. "
        "Salary EUR 70,000 - 90,000 per year.")
BOARD_WITH_TEXT = {"jobs": [
    {"id": 4012345, "title": "Senior ML Engineer", "location": {"name": "Remote - Spain"},
     "absolute_url": POSTING, "content": html.escape(f"<p>{TEXT}</p>")},
    {"id": 4012349, "title": "Staff Machine Learning Engineer", "location": {"name": "Remote - EU"},
     "absolute_url": "https://boards.greenhouse.io/acme/jobs/4012349", "content": html.escape(f"<p>{TEXT}</p>")},
]}


@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    offline(tmp_path, monkeypatch)


def _serve(extra):
    def serving(request):
        answer = extra(request)
        return answer if answer is not None else handler(request)
    http_factory.set_test_overrides(transport=httpx.MockTransport(serving), resolver=lambda h, p: ["93.184.216.34"],
                                    sleep=lambda s: None, min_interval_per_host=0.0)


def test_host_gate_keeps_one_request_per_host_at_a_time():
    gate, active, peak, lock = HostGate(), [0], [0], threading.Lock()

    def hit():
        with gate.lock_for("api.example.com"):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.01)
            with lock:
                active[0] -= 1
    threads = [threading.Thread(target=hit) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 1
    slept = []
    gate.space("h", 1.0, lambda: 10.0, slept.append)
    gate.space("h", 1.0, lambda: 10.25, slept.append)
    assert slept == [0.75]


def test_a_shared_vendor_budget_counts_every_thread():
    budget = VendorBudget(max_requests=1000)
    threads = [threading.Thread(target=lambda: [budget.charge("greenhouse") for _ in range(100)]) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert budget.requests["greenhouse"] == 800


def test_employer_names_never_name_the_platform():
    assert dossier.employer_name("Manatal", "https://careers-page.com/acme-labs/job/123") == "Acme Labs"
    assert dossier.employer_name("Job Boards", "https://job-boards.greenhouse.io/nebius/jobs/1") == "Nebius"
    assert dossier.employer_name("", "https://apply.workable.com/huggingface/j/ABC123/") == "Huggingface"
    assert dossier.employer_name("Real Co", "https://example.org/x") == "Real Co"


def test_salary_reads_one_or_two_bounds(monkeypatch):
    assert dossier.salary(TEXT) == "EUR 70000-90000 annual"
    monkeypatch.setattr(dossier, "extract_salaries_from_text",
                        lambda _t: [{"currency": "EUR", "low": None, "high": 120000.0, "period": "annual"}])
    assert dossier.salary("up to EUR 120k") == "EUR 120000 annual"
    monkeypatch.setattr(dossier, "extract_salaries_from_text",
                        lambda _t: [{"currency": "EUR", "low": None, "high": None, "period": "annual"}])
    assert dossier.salary("x") == ""


def test_api_terms_drop_seniority_words():
    assert role_feeds.api_terms(["Senior AI Engineer", "Lead FP&A Analyst", "Head of Finance"]) == [
        "AI Engineer", "FP&A Analyst", "Finance"]
    assert role_feeds.api_terms(["Senior"]) == ["Senior"]


class _Page:
    def __init__(self, links):
        self.links = links

    def get_text(self, url, **_kwargs):
        body = "".join(f'<a href="{link}">x</a>' for link in self.links)
        return 200, url, f"<html><body>{body}</body></html>"


def test_aggregators_resolve_to_employer_sites_and_respect_the_company():
    page = _Page(["https://acme.com/careers/senior-ml-engineer", "https://www.linkedin.com/company/acme",
                  "https://remoteok.com/remote-jobs/2"])
    resolved = scout_resolve.resolve_aggregator("https://remoteok.com/1", page, 5, "Acme")
    assert (resolved.outcome, resolved.url, resolved.source_type) == (
        "resolved_aggregator", "https://acme.com/careers/senior-ml-engineer", "employer_page")
    other = scout_resolve.resolve_aggregator("https://remoteok.com/1", page, 5, "Other Corp")
    assert other.outcome == "unresolved_aggregator"
    single = scout_resolve.resolve_aggregator("https://remoteok.com/1", _Page([POSTING]), 5, "Other Corp")
    assert single.outcome == "unresolved_aggregator"  # one related job of another employer is not this find


def test_greenhouse_postings_are_read_from_their_board_with_siblings():
    page_hits = []

    def extra(request):
        if "greenhouse" in request.url.host and request.url.path.startswith("/v1/boards/acme"):
            assert request.url.params.get("content") == "true"
            return httpx.Response(200, json=BOARD_WITH_TEXT)
        if request.url.host == "boards.greenhouse.io":
            page_hits.append(str(request.url))
        return None
    _serve(extra)
    result = inspect.inspect_jobs(SLUG, [{"url": POSTING, "company": "Acme"}])
    by_id = {d["id"]: d for d in result["dossiers"]}
    read = by_id["greenhouse:acme:4012345"]
    assert read["status"] == "open" and "via" not in read and read["salary"] == "EUR 70000-90000 annual"
    assert "LLM" in read["role_excerpt"] and read["geo_excerpt"]
    assert by_id["greenhouse:acme:4012349"]["via"] == "same_board"
    assert page_hits == []  # one board request read the posting; no page was scraped


def test_replies_are_compact_unless_asked():
    gone = "https://careers.example.org/jobs/closed-role"
    dead = "https://remoteok.com/remote-jobs/999-unknown"
    result = inspect.inspect_jobs(SLUG, [EMPLOYER, gone, {"url": dead, "company": "Ghost Co"}], ["Nobody Widgets"])
    assert {d["status"] for d in result["dossiers"]} == {"open"}
    assert result["status"]["closed"] == 1 and result["status"]["no_board"] == 1
    assert result["unresolved"] == [{"url": dead, "company": "Ghost Co"}]
    assert result["companies_without_matches"] == [{"company": "Nobody Widgets", "status": "no_board"}]
    full = inspect.inspect_jobs(SLUG, [EMPLOYER, gone], show_all=True)
    assert {d["status"] for d in full["dossiers"]} == {"open", "closed"}


def test_saved_rows_get_a_company_from_the_url():
    folder = inspect.home.ultron_home() / inspect.CACHE_DIR / SLUG
    folder.mkdir(parents=True)
    url = "https://careers-page.com/acme-labs/job/ABC123"
    row = {"identity_key": "url://careers-page.com/acme-labs/job/ABC123", "status": "open", "url": url,
           "source_type": "web_search", "source_key": "",
           "job": {"apply_url": url, "title": "FP&A Manager", "company": "Manatal", "location": "Remote"}}
    (folder / inspect.CACHE_FILE).write_text(json.dumps(row) + "\n", encoding="utf-8")
    filled = shortlist._with_cache([{"id": row["identity_key"], "verdict": "keep"}], inspect.cached_postings(SLUG))
    assert filled[0]["company"] == "Acme Labs"


def test_hn_rows_use_the_headline_as_title(monkeypatch):
    def collector(_client, _source, _timeout):
        return [{"title": "Remote", "company_name": "Acme", "url": "https://acme.com/jobs/1",
                 "headline": "Acme | Senior ML Engineer | Remote EU", "description": "We build LLM systems."}], []
    monkeypatch.setitem(role_feeds.COLLECTORS, "hn_who_is_hiring", collector)
    status, rows = role_feeds.local_feed_rows({"id": "hn_who_is_hiring", "url": "https://hn.example/x",
                                               "parser": "hn_who_is_hiring"}, None, 5)
    assert status["status"] == "ok" and rows[0]["title"] == "Acme | Senior ML Engineer | Remote EU"
    assert rows[0]["description"] == "We build LLM systems."


def test_a_greenhouse_board_too_large_with_text_is_listed_without_it():
    asked = []

    def extra(request):
        if "greenhouse" in request.url.host and request.url.path.startswith("/v1/boards/acme"):
            asked.append(request.url.params.get("content"))
            if request.url.params.get("content") == "true":
                return httpx.Response(200, text="{truncated")  # what a cut-off oversized reply parses as
        return None
    _serve(extra)
    result = inspect.inspect_jobs(SLUG, ["https://boards.greenhouse.io/acme"])
    assert "true" in asked and "false" in asked
    assert [d["status"] for d in result["dossiers"]] and all(d["status"] == "listed" for d in result["dossiers"])


def test_requested_postings_match_their_board_whatever_the_greenhouse_host():
    page_hits = []

    def extra(request):
        if "greenhouse" in request.url.host and request.url.path.startswith("/v1/boards/acme"):
            return httpx.Response(200, json=BOARD_WITH_TEXT)
        if request.url.host in {"boards.greenhouse.io", "job-boards.greenhouse.io"} and "/jobs/" in request.url.path:
            page_hits.append(str(request.url))
        return None
    _serve(extra)
    result = inspect.inspect_jobs(SLUG, ["https://job-boards.greenhouse.io/acme/jobs/4012345"])
    read = {d["id"]: d for d in result["dossiers"]}["greenhouse:acme:4012345"]
    assert read["status"] == "open" and "via" not in read and page_hits == []


def test_the_gate_spaces_tenants_of_one_vendor_across_clients():
    from ultron.infra.net import HttpClient

    gate, sleeps = HostGate(), []
    clock = [100.0]

    def make():
        return HttpClient(max_requests=10, max_requests_per_host=10, min_interval_per_host=0.0,
                          transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
                          resolver=lambda h, p: ["93.184.216.34"], sleep=sleeps.append, clock=lambda: clock[0],
                          gate=gate, vendor_of=lambda host: "workday" if "myworkdayjobs" in host else None,
                          min_interval_per_vendor=2.0, vendor_budget=VendorBudget(max_requests=None))
    make().request_json("https://a.wd1.myworkdayjobs.com/x")
    make().request_json("https://b.wd3.myworkdayjobs.com/y")  # another tenant, another client
    assert 2.0 in sleeps


def test_an_explicit_read_wins_over_its_board_listing():
    listed = {"id": "k", "status": "listed", "board": "workable:acme"}
    read = {"id": "k", "status": "open", "hint": {}}
    sibling = {"id": "k", "status": "open", "via": "same_board"}
    assert inspect._unique([listed, read]) == [read]
    assert inspect._unique([sibling, read]) == [read]
    assert inspect._unique([read, listed]) == [read]


def test_a_stored_guessed_board_stays_unconfirmed(monkeypatch):
    from ultron.core.models import AtsBoardSeed
    from ultron.toolkit import boards
    guessed = AtsBoardSeed(seed_id="greenhouse:acme", company="Acme", ats="greenhouse", board_identifier="acme",
                           source_type="guessed_unconfirmed")
    monkeypatch.setattr(boards, "inventory_board", lambda name: guessed)
    dossiers = inspect.inspect_jobs(SLUG, [], ["Acme"])["dossiers"]
    assert dossiers and all(d.get("board_confirmed") is False for d in dossiers)


def test_board_texts_keep_one_line_per_paragraph():
    from ultron.core.ats.clients.greenhouse import _content_text
    from ultron.core.ats.clients.lever import _lever_text
    text = _content_text(html.escape("<p>Intro paragraph.</p><p>Remote within Spain only.</p>"))
    assert text.splitlines() == ["Intro paragraph.", "Remote within Spain only."]
    lever = _lever_text({"descriptionPlain": "About us.", "lists": [{"text": "Where", "content": "<li>EU only</li>"}]})
    assert "EU only" in lever.splitlines()


def test_co_uk_employer_links_are_not_the_aggregator():
    page = _Page(["https://acme.co.uk/careers/ml-engineer"])
    found = scout_resolve.resolve_aggregator("https://www.reed.co.uk/jobs/77", page, 5, "Acme")
    assert found.outcome == "resolved_aggregator" and found.url == "https://acme.co.uk/careers/ml-engineer"


def test_an_unresolved_listing_falls_back_to_its_company_board():
    assert inspect.aggregator_company("https://himalayas.app/companies/acme/jobs/ml-lead") == "Acme"
    dead = "https://remoteok.com/remote-jobs/999-unknown"
    result = inspect.inspect_jobs(SLUG, [{"url": dead, "company": "Acme"}])
    via = [d for d in result["dossiers"] if d.get("via") == "company_of_listing"]
    assert via and via[0]["found_at"] == dead and via[0]["board"] == "greenhouse:acme"


def test_new_only_keeps_a_new_opening_behind_a_seen_one():
    from ultron.toolkit import search
    seen = {"id": "a", "url": "https://x.example/a", "status": "listed", "title": "ML Lead", "company": "Acme",
            "location": "", "source": "s"}
    new = {**seen, "id": "b", "url": "https://x.example/b"}
    ranked = search._rank([seen, new], ["ML Lead"])
    assert [d["id"] for d in ranked] == ["a"]  # one per role when both are kept
    flags = {"a": "rejected", "b": "new"}
    kept = [d for d in [seen, new] if flags[d["id"]] == "new"]
    assert [d["id"] for d in search._rank(kept, ["ML Lead"])] == ["b"]


def test_a_confirmed_company_board_joins_the_profile_inventory(monkeypatch):
    from ultron import seeds
    from ultron.toolkit import boards
    monkeypatch.setattr(boards, "guessed_board_ownership", lambda *_a: "owned")
    inspect.inspect_jobs(SLUG, [], ["Acme"])
    assert any(s.board_identifier == "acme" for s in seeds.load_inventory(SLUG))


def test_board_reads_keep_date_provenance_and_closure():
    from ultron.core.ats.clients.base import AtsPosting
    from ultron.core.models import AtsBoardSeed
    from ultron.toolkit import boards
    seed = AtsBoardSeed(seed_id="greenhouse:acme", company="Acme", ats="greenhouse", board_identifier="acme")
    edited = AtsPosting(title="ML Lead", apply_url=POSTING, posted_at="2026-11-20", description="Remote EU.",
                        raw_payload={"updated_at": "2026-11-20"})
    assert boards.board_job(seed, edited).posting_date is None  # an edit date is not a publication date
    filled = AtsPosting(title="ML Lead", apply_url=POSTING, description="This position has been filled.")
    assert boards.board_status(boards.board_job(seed, filled)) == "closed"
    assert boards.board_status(boards.board_job(seed, edited)) == "open"
