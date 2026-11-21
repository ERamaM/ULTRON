"""Apply URL selection: final URL, then rel=canonical, then the candidate URL, first one on the ATS allowlist."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ultron import seeds
from ultron.core.decision.unit import load_allowed_domains
from ultron.core.models import CandidateUrl
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.stages.fetch import fetch, page_canonical_url, resolve_apply_url
from ultron.storage import runs

PAGE = (Path(__file__).resolve().parents[1] / "fixtures" / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")
ALLOWED = load_allowed_domains()


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)


def _cand(url: str, source_key: str = "", source_type: str = "ats_api", query: str = "") -> CandidateUrl:
    return CandidateUrl(query_id=source_key or "web:1", query=query, source_url=url, normalized_url=url,
                        domain=url.split("/")[2], source_type=source_type, source_key=source_key)


def _with_canonical(href: str) -> str:
    return PAGE.replace("<head>", f'<head><link rel="canonical" href="{href}">', 1)


@pytest.mark.parametrize(("html", "expected"), [
    ('<link rel="canonical" href="https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555">',
     "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555"),
    ('<link href="/jobs/42" rel="canonical">', "https://careers.example.test/jobs/42"),
    ('<link rel="alternate canonical" href="https://careers.example.test/x">', "https://careers.example.test/x"),
    ('<link rel="canonical" href="javascript:alert(1)">', None),
    ('<link rel="canonical" href="">', None),
    ('<link rel="stylesheet" href="/a.css">', None),
    ("", None),
])
def test_page_canonical_url(html, expected):
    assert page_canonical_url(f"<html><head>{html}</head></html>", "https://careers.example.test/opening") == expected


def test_allowlisted_final_url_wins():
    cand = _cand("https://boards.greenhouse.io/acme/jobs/1", "greenhouse:acme")
    url = resolve_apply_url(cand, "https://job-boards.greenhouse.io/acme/jobs/1",
                            "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555", ALLOWED)
    assert url == "https://job-boards.greenhouse.io/acme/jobs/1"


def test_canonical_used_when_final_url_is_a_custom_domain():
    cand = _cand("https://careers.example.test/jobs/1", source_type="web_search", source_key="web_search")
    url = resolve_apply_url(cand, "https://careers.example.test/jobs/1",
                            "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555", ALLOWED)
    assert url == "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555"


def test_canonical_pointing_at_a_board_root_is_ignored():
    # A page-controlled canonical to a board root would collapse every posting into one identity.
    cand = _cand("https://careers.example.test/jobs/1", source_type="web_search", source_key="web_search")
    url = resolve_apply_url(cand, "https://careers.example.test/jobs/1", "https://boards.greenhouse.io/acme", ALLOWED)
    assert url == "https://careers.example.test/jobs/1"


@pytest.mark.parametrize("final_url", ["https://www.linkedin.com/jobs/view/123", "https://de.indeed.com/viewjob?jk=1"])
def test_blocked_or_aggregator_final_host_is_never_replaced(final_url):
    # A page on a blocked or aggregator host must stay discardable, whatever its canonical link says.
    lever = "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555"
    cand = _cand(lever, "lever:acme")
    assert resolve_apply_url(cand, final_url, lever, ALLOWED) == final_url


def test_canonical_with_credentials_is_ignored():
    assert page_canonical_url('<link rel="canonical" href="https://user:pw@jobs.lever.co/acme/x">',
                              "https://careers.example.test/") is None


def test_candidate_url_used_when_page_redirected_off_the_allowlist():
    cand = _cand("https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555", "lever:acme")
    url = resolve_apply_url(cand, "https://www.acme.test/careers", None, ALLOWED)
    assert url == "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555"


def test_custom_domain_gh_jid_is_canonicalized_to_greenhouse():
    cand = _cand("https://careers.acme.test/open-roles?gh_jid=4012345", "greenhouse:acme")
    url = resolve_apply_url(cand, "https://careers.acme.test/open-roles?gh_jid=4012345", None, ALLOWED)
    assert url == "https://boards.greenhouse.io/acme/jobs/4012345"


def test_gh_jid_without_a_known_board_keeps_the_final_url():
    cand = _cand("https://careers.acme.test/open-roles?gh_jid=4012345", source_type="web_search",
                 source_key="web_search")
    url = resolve_apply_url(cand, "https://careers.acme.test/open-roles?gh_jid=4012345", None, ALLOWED)
    assert url == "https://careers.acme.test/open-roles?gh_jid=4012345"


def _run(handler) -> str:
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    return runs.new_run(profiles.EXAMPLE_SLUG, "balanced")


def test_fetch_uses_rel_canonical_for_a_custom_domain_page():
    lever = "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555"
    run_id = _run(lambda r: httpx.Response(200, text=_with_canonical(lever)))
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [_cand("https://careers.example.test/jobs/7", "web_search",
                                                    source_type="web_search")])
    runs.mark_stage(run_id, "discover_ats")
    assert fetch(run_id)["fetched"] == 1
    job = runs.read_jsonl(d / "postings.jsonl")[0]["job"]
    assert job["apply_url"] == lever and job["domain"] == "jobs.lever.co"


def test_fetch_canonicalizes_custom_domain_gh_jid_to_greenhouse():
    run_id = _run(lambda r: httpx.Response(200, text=PAGE))
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [_cand("https://careers.acme.test/open-roles?gh_jid=4012345",
                                                    "greenhouse:acme")])
    runs.mark_stage(run_id, "discover_ats")
    assert fetch(run_id)["fetched"] == 1
    job = runs.read_jsonl(d / "postings.jsonl")[0]["job"]
    assert job["apply_url"] == "https://boards.greenhouse.io/acme/jobs/4012345"
    assert job["domain"] == "boards.greenhouse.io"


def test_enricher_apply_url_is_not_overridden():
    # SmartRecruiters' careers subdomain redirected to the employer homepage: the enricher's posting URL stays.
    long_home = "<html><head><title>Acme</title></head><body>" + "<p>Acme builds things.</p>" * 40 + "</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "careers.smartrecruiters.com":
            return httpx.Response(302, headers={"Location": "https://www.acme.test/"})
        if request.url.host == "www.acme.test":
            return httpx.Response(200, text=long_home)
        if request.url.host == "api.smartrecruiters.com":
            return httpx.Response(200, json={"name": "Senior Data Engineer", "company": {"name": "Acme"},
                                             "jobAd": {"sections": {"jobDescription": {"text": "<p>Build data.</p>"}}}})
        return httpx.Response(404)

    run_id = _run(handler)
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [_cand("https://careers.smartrecruiters.com/Acme/7440001",
                                                    "smartrecruiters:Acme")])
    runs.mark_stage(run_id, "discover_ats")
    assert fetch(run_id)["fetched"] == 1
    job = runs.read_jsonl(d / "postings.jsonl")[0]["job"]
    assert job["title"] == "Senior Data Engineer"
    assert job["apply_url"] == "https://jobs.smartrecruiters.com/Acme/7440001"
