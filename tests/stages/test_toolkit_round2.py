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
