from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ultron import seeds
from ultron.cli import main
from ultron.core.models import CandidateUrl
from ultron.profiles import store as profiles
from ultron.stages import http_factory
from ultron.storage import runs

PAGE = (Path(__file__).resolve().parents[1] / "fixtures" / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: empty)


def _run(handler):
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )
    return runs.new_run(profiles.EXAMPLE_SLUG, "balanced")


def _cand(url, **kw):
    return CandidateUrl(query_id="greenhouse:acme", query="", source_url=url, normalized_url=url,
                        domain="boards.greenhouse.io", source_type="ats_api", source_key="greenhouse:acme", **kw)


def test_fetch_requires_discover(capsys):
    run_id = _run(lambda r: httpx.Response(200, text=PAGE))
    assert main(["fetch", "--run", run_id]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "stage_order"


def test_fetch_extracts_title_company(capsys):
    def handler(request):
        if request.url.path.endswith("/1"):
            return httpx.Response(200, text=PAGE)
        return httpx.Response(404)

    run_id = _run(handler)
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [
        _cand("https://boards.greenhouse.io/acme/jobs/1", ats_title="Senior ML Engineer", ats_company="Acme"),
        _cand("https://boards.greenhouse.io/acme/jobs/2"),
    ])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fetched"] == 1 and out["failed"] == 1 and out["requests"] == 2
    row = runs.read_jsonl(d / "postings.jsonl")[0]
    assert row["job"]["title"] and row["job"]["company"] and row["identity_key"]
    assert row["candidate"]["source_key"] == "greenhouse:acme"
    assert runs.read_jsonl(d / "fetch_errors.jsonl")[0]["error"] == "expired_or_closed"


def _cands(source_key, n, host="boards.greenhouse.io"):
    return [
        CandidateUrl(query_id=source_key, query="", source_url=f"https://{host}/{source_key.split(':')[1]}/jobs/{i}",
                     normalized_url=f"https://{host}/{source_key.split(':')[1]}/jobs/{i}", domain=host,
                     source_type="ats_api", source_key=source_key)
        for i in range(1, n + 1)
    ]


def test_fetch_round_robin_selection(capsys):
    from ultron.stages.fetch import fetch

    run_id = _run(lambda r: httpx.Response(200, text=PAGE))
    d = runs.run_dir(run_id)
    web = CandidateUrl(query_id="web:1", query="", source_url="https://example.org/job/9",
                       normalized_url="https://example.org/job/9", domain="example.org",
                       source_type="web_search", source_key="web_search")
    runs.write_jsonl(d / "candidates.jsonl", [*_cands("greenhouse:a", 5), *_cands("greenhouse:b", 5), web])
    runs.mark_stage(run_id, "discover_ats")
    http_factory.set_test_overrides(max_requests=100)
    run = runs.load_run(run_id)
    run["settings"]["max_pages"] = 4
    runs._write_run(run_id, run)
    assert fetch(run_id)["fetched"] == 4
    keys = [r["candidate"]["source_key"] for r in runs.read_jsonl(d / "postings.jsonl")]
    assert keys == ["greenhouse:a", "greenhouse:b", "web_search", "greenhouse:a"]


def test_fetch_not_limited_by_per_host_cap():
    from ultron.stages.fetch import fetch

    run_id = _run(lambda r: httpx.Response(200, text=PAGE))
    d = runs.run_dir(run_id)
    run = runs.load_run(run_id)
    run["settings"]["max_requests_per_host"] = 10
    runs._write_run(run_id, run)
    runs.write_jsonl(d / "candidates.jsonl", _cands("greenhouse:a", 14))
    runs.mark_stage(run_id, "discover_ats")
    assert runs.load_run(run_id)["settings"]["max_requests_per_host"] == 10
    res = fetch(run_id)
    assert res["fetched"] == 14 and res["failed"] == 0


def test_fetch_global_budget_stop_recorded():
    from ultron.stages.fetch import fetch

    run_id = _run(lambda r: httpx.Response(200, text=PAGE))
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", _cands("greenhouse:a", 4))
    runs.mark_stage(run_id, "discover_ats")
    http_factory.set_test_overrides(max_requests=2)
    res = fetch(run_id)
    assert res["fetched"] == 2 and res["failed"] == 2
    errs = runs.read_jsonl(d / "fetch_errors.jsonl")
    assert errs[0]["error"] == "request_budget_exhausted" and errs[1]["error"] == "skipped_request_budget_exhausted"


def test_fetch_cleans_job_text(capsys):
    page = PAGE.replace("Senior ML Engineer", "Senior R&amp;D Engineerâ€™s")
    run_id = _run(lambda r: httpx.Response(200, text=page))
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [
        _cand("https://boards.greenhouse.io/acme/jobs/1", ats_title="Senior R&amp;D Engineer", ats_company="A&amp;B")])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    job = runs.read_jsonl(d / "postings.jsonl")[0]["job"]
    for field in ("title", "company", "location"):
        assert "&amp;" not in job[field] and "&#" not in job[field] and "â€" not in job[field]


def test_fetch_greenhouse_429_falls_back_to_json_api(capsys):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "boards.greenhouse.io":
            return httpx.Response(429)
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json={
                "title": "Senior ML Engineer",
                "company_name": "Helsing",
                "location": {"name": "Remote - Europe"},
                "content": "<p>Build production LLM inference pipelines.</p>",
                "absolute_url": "https://boards.greenhouse.io/helsing/jobs/123",
                "updated_at": "2026-11-18T10:00:00Z",
                "first_published": "2026-11-10T10:00:00Z",
            })
        return httpx.Response(404)

    run_id = _run(handler)
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [_cand("https://boards.greenhouse.io/helsing/jobs/123")])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    postings = runs.read_jsonl(d / "postings.jsonl")
    assert len(postings) == 1
    assert postings[0]["job"]["company"] == "Helsing"
    assert postings[0]["job"]["posting_date"] == "2026-11-10T10:00:00Z"
    assert "production LLM inference" in postings[0]["job"]["body_text"]


def test_fetch_workable_spa_enriches_from_v2_api(capsys):
    spa_html = "<html><head><title>Senior ML Engineer - EMEA Remote - Hugging Face</title></head><body><div id='root'></div></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if "/api/v2/accounts/huggingface/jobs/9E2A4C02C7" in str(request.url):
            return httpx.Response(200, json={
                "title": "Senior ML Engineer, Voice Agents",
                "remote": True,
                "location": {"country": "France", "city": "Paris"},
                "locations": [{"country": "France", "city": "Paris"}, {"country": "United Kingdom"}],
                "description": "<p>Own open-source speech-to-speech LLM pipelines at scale.</p>",
                "requirements": "<ul><li>5+ years PyTorch and GPU serving</li></ul>",
                "published": "2026-11-15",
            })
        return httpx.Response(200, text=spa_html)

    run_id = _run(handler)
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [
        CandidateUrl(
            query_id="workable:huggingface", query="",
            source_url="https://apply.workable.com/j/9E2A4C02C7",
            normalized_url="https://apply.workable.com/j/9E2A4C02C7",
            domain="apply.workable.com", source_type="ats_api", source_key="workable:huggingface",
            ats_company="Hugging Face",
        )
    ])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    job = runs.read_jsonl(d / "postings.jsonl")[0]["job"]
    assert "speech-to-speech LLM pipelines" in job["body_text"]
    assert "PyTorch" in job["body_text"]
    assert "Remote" in job["remote_text"]
    assert job["location"] == "Paris, France; United Kingdom"


def test_fetch_workable_closed_job_redirecting_to_live_board(capsys):
    url = "https://apply.workable.com/acme/j/1234AB"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/acme/j/1234AB":
            return httpx.Response(302, headers={"Location": "/acme/?not_found=true"})
        return httpx.Response(200, text="<h1>Acme</h1><p>See our current job openings.</p>")

    run_id = _run(handler)
    folder = runs.run_dir(run_id)
    runs.write_jsonl(folder / "candidates.jsonl", [_cand(url)])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    assert not runs.read_jsonl(folder / "postings.jsonl")
    assert runs.read_jsonl(folder / "fetch_errors.jsonl")[0]["error"] == "expired_or_closed"


@pytest.mark.parametrize("host", ["job-boards.greenhouse.io", "boards.greenhouse.io", "job-boards.eu.greenhouse.io"])
def test_fetch_greenhouse_closed_job_redirecting_to_board_error(capsys, host):
    url = f"https://{host}/acme/jobs/4012345"
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/acme/jobs/4012345":
            return httpx.Response(302, headers={"Location": "/acme?error=true"})
        return httpx.Response(200, text="<h1>Acme</h1><p>Current openings: Senior ML Engineer.</p>")

    run_id = _run(handler)
    folder = runs.run_dir(run_id)
    runs.write_jsonl(folder / "candidates.jsonl", [_cand(url)])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    assert not runs.read_jsonl(folder / "postings.jsonl")
    assert runs.read_jsonl(folder / "fetch_errors.jsonl")[0]["error"] == "expired_or_closed"
    assert not any("boards-api" in request for request in seen)


def test_fetch_greenhouse_board_error_param_without_a_job_redirect_is_not_closed():
    from ultron.stages.fetch import _greenhouse_closed_redirect

    assert _greenhouse_closed_redirect(
        "https://job-boards.greenhouse.io/acme/jobs/1", "https://job-boards.greenhouse.io/acme?error=true"
    )
    assert not _greenhouse_closed_redirect(
        "https://job-boards.greenhouse.io/acme", "https://job-boards.greenhouse.io/acme?error=true"
    )
    assert not _greenhouse_closed_redirect(
        "https://job-boards.greenhouse.io/acme/jobs/1", "https://job-boards.greenhouse.io/acme/jobs/1?error=false"
    )
    assert not _greenhouse_closed_redirect(
        "https://careers.acme.example/jobs/1", "https://careers.acme.example/acme?error=true"
    )


def test_fetch_smartrecruiters_reads_detail_page_location_and_workplace(capsys):
    url = "https://jobs.smartrecruiters.com/Acme/1234567"
    page = """
    <h1>Process Engineer</h1>
    <spl-job-location formattedAddress="Madrid, Spain" workplaceType="hybrid"></spl-job-location>
    <meta itemprop="datePosted" content="2026-10-01">
    <p>On-site training is available for all departments.</p>
    """ + "<p>Coordinate manufacturing process improvement and optimize yield.</p>" * 10
    run_id = _run(lambda request: httpx.Response(200, text=page))
    folder = runs.run_dir(run_id)
    runs.write_jsonl(folder / "candidates.jsonl", [_cand(url)])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    job = runs.read_jsonl(folder / "postings.jsonl")[0]["job"]
    assert (job["location"], job["remote_text"], job["posting_date"]) == ("Madrid, Spain", "Hybrid", "2026-10-01")


def test_fetch_greenhouse_custom_domain_spa_enriches_from_api(capsys):
    spa_html = "<html><head><title>Principal AI Engineer - Elastic</title></head><body><div id='root'>Elastic</div></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "job-boards.greenhouse.io":
            return httpx.Response(302, headers={"Location": "https://jobs.elastic.co/jobs/ai/spain/123?gh_jid=123"})
        if request.url.host == "jobs.elastic.co":
            return httpx.Response(200, text=spa_html)
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(200, json={
                "title": "Principal AI Engineer",
                "company_name": "Elastic",
                "location": {"name": "Spain"},
                "content": "<p>Build distributed vector search and agentic RAG at Elastic in Spain.</p>",
                "absolute_url": "https://jobs.elastic.co/jobs/ai/spain/123?gh_jid=123",
                "updated_at": "2026-11-18T12:00:00Z",
            })
        return httpx.Response(404)

    run_id = _run(handler)
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [
        CandidateUrl(
            query_id="greenhouse:elastic", query="",
            source_url="https://job-boards.greenhouse.io/elastic/jobs/123",
            normalized_url="https://job-boards.greenhouse.io/elastic/jobs/123",
            domain="job-boards.greenhouse.io", source_type="ats_api", source_key="greenhouse:elastic",
            ats_company="Elastic",
        )
    ])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    row = runs.read_jsonl(d / "postings.jsonl")[0]
    assert row["ats_fingerprint"] is True
    assert "agentic RAG at Elastic" in row["job"]["body_text"]
    assert row["job"]["posting_date"] is None
    assert row["job"]["updated_at"] == "2026-11-18T12:00:00Z"


def test_fetch_smartrecruiters_wrapper_redirect_and_workday_spa_enrich(capsys):
    sr_wrapper = "<html><head><title>Careers at Bosch</title></head><body><h1>Careers at Bosch</h1><p>Explore Bosch worldwide.</p></body></html>"
    wd_spa = "<html><head><title></title></head><body><div id='root'></div></body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "careers.smartrecruiters.com":
            return httpx.Response(302, headers={"Location": "https://jobs.bosch.com/en"})
        if request.url.host == "jobs.bosch.com":
            return httpx.Response(200, text=sr_wrapper)
        if request.url.host == "api.smartrecruiters.com" and "/postings/7440001" in request.url.path:
            return httpx.Response(200, json={
                "name": "Senior Process Engineer Polymers",
                "company": {"name": "Bosch Group"},
                "location": {"city": "Madrid", "country": "Spain", "remote": False},
                "releasedDate": "2026-11-10",
                "jobAd": {"sections": {
                    "jobDescription": {"text": "<p>Lead polymers extrusion process scale-up in Madrid plant.</p>"},
                    "qualifications": {"text": "<ul><li>5+ years in industrial process engineering</li></ul>"},
                }},
            })
        if request.url.host == "dupont.wd5.myworkdayjobs.com" and "/wday/cxs/" in request.url.path:
            return httpx.Response(200, json={
                "jobPostingInfo": {
                    "title": "Tyvek R&D Product Development Engineer",
                    "location": "Contern, Luxembourg",
                    "remoteType": "On-site",
                    "postedOn": "Posted Today",
                    "jobDescription": "<p>Develop nonwoven Tyvek materials and run plant scale-up trials.</p>",
                }
            })
        if request.url.host == "dupont.wd5.myworkdayjobs.com":
            return httpx.Response(200, text=wd_spa)
        return httpx.Response(404)

    run_id = _run(handler)
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [
        CandidateUrl(
            query_id="smartrecruiters:BoschGroup", query="",
            source_url="https://careers.smartrecruiters.com/BoschGroup/7440001",
            normalized_url="https://careers.smartrecruiters.com/BoschGroup/7440001",
            domain="careers.smartrecruiters.com", source_type="ats_api", source_key="smartrecruiters:BoschGroup",
            ats_company="Bosch Group",
        ),
        CandidateUrl(
            query_id="workday:dupont.wd5.myworkdayjobs.com/jobs", query="",
            source_url="https://dupont.wd5.myworkdayjobs.com/jobs/job/Contern-Luxembourg/Tyvek_R-1",
            normalized_url="https://dupont.wd5.myworkdayjobs.com/jobs/job/Contern-Luxembourg/Tyvek_R-1",
            domain="dupont.wd5.myworkdayjobs.com", source_type="ats_api",
            source_key="workday:dupont.wd5.myworkdayjobs.com/jobs", ats_company="DuPont",
        ),
    ])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    by_company = {r["job"]["company"]: r for r in runs.read_jsonl(d / "postings.jsonl")}
    sr = by_company["Bosch Group"]["job"]
    assert sr["title"] == "Senior Process Engineer Polymers"
    assert "Madrid" in sr["location"] and "polymers extrusion" in sr["body_text"]
    assert sr["apply_url"] == "https://jobs.smartrecruiters.com/BoschGroup/7440001"
    assert sr["domain"] == "jobs.smartrecruiters.com"
    assert by_company["Bosch Group"]["identity_key"] == "smartrecruiters:boschgroup:7440001"
    wd = by_company["DuPont"]["job"]
    assert wd["title"] == "Tyvek R&D Product Development Engineer"
    assert "nonwoven Tyvek materials" in wd["body_text"]


def test_fetch_bamboohr_spa_and_greenhouse_form_shell_enrich_from_api(capsys):
    bamboo_html = "<html><head><title>BambooHR</title></head><body><div id='app'>BambooHR</div></body></html>"
    gh_form_html = (
        "<html><head><title>Job Application for Senior Software Engineer at FeverUp</title></head>"
        "<body><h1>Job Application for Senior Software Engineer at FeverUp</h1>"
        "<p>Apply for this job * indicates a required field First Name * Last Name * Email * Phone Country * Resume/CV *</p>"
        "</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "vizzuality.bamboohr.com" and request.url.path == "/careers/24/detail":
            return httpx.Response(200, json={
                "result": {"jobOpening": {
                    "jobOpeningName": "Software Engineer (Python/Django)",
                    "location": {"city": "Remote", "state": "Spain", "addressCountry": "Spain"},
                    "datePosted": "2026-11-12",
                    "description": "<p>Build geospatial Python APIs for global biodiversity monitoring.</p>",
                }}
            })
        if request.url.host == "vizzuality.bamboohr.com":
            return httpx.Response(200, text=bamboo_html)
        if request.url.host == "boards-api.greenhouse.io" and "/feverup/jobs/4783" in request.url.path:
            return httpx.Response(200, json={
                "title": "Senior Software Engineer",
                "company_name": "Fever",
                "location": {"name": "Madrid, Spain"},
                "content": "<p>Build high-scale event ticketing microservices in Python and PostgreSQL in Madrid.</p>",
                "absolute_url": "https://job-boards.greenhouse.io/feverup/jobs/4783",
            })
        if request.url.host == "job-boards.greenhouse.io":
            return httpx.Response(200, text=gh_form_html)
        return httpx.Response(404)

    run_id = _run(handler)
    d = runs.run_dir(run_id)
    runs.write_jsonl(d / "candidates.jsonl", [
        CandidateUrl(
            query_id="bamboohr:vizzuality", query="",
            source_url="https://vizzuality.bamboohr.com/careers/24",
            normalized_url="https://vizzuality.bamboohr.com/careers/24",
            domain="vizzuality.bamboohr.com", source_type="ats_api",
            source_key="bamboohr:vizzuality", ats_company="Vizzuality",
        ),
        CandidateUrl(
            query_id="greenhouse:feverup", query="",
            source_url="https://job-boards.greenhouse.io/feverup/jobs/4783",
            normalized_url="https://job-boards.greenhouse.io/feverup/jobs/4783",
            domain="job-boards.greenhouse.io", source_type="ats_api",
            source_key="greenhouse:feverup", ats_company="Fever",
        ),
    ])
    runs.mark_stage(run_id, "discover_ats")
    assert main(["fetch", "--run", run_id]) == 0
    by_company = {r["job"]["company"]: r for r in runs.read_jsonl(d / "postings.jsonl")}
    assert "geospatial Python APIs" in by_company["Vizzuality"]["job"]["body_text"]
    assert by_company["Vizzuality"]["job"]["posting_date"] == "2026-11-12"
    assert "microservices in Python" in by_company["Fever"]["job"]["body_text"]
    assert by_company["Fever"]["ats_fingerprint"] is True


def test_fetch_invalid_idna_host_is_recorded_and_stage_continues():
    from ultron.stages.fetch import fetch

    run_id = _run(lambda r: httpx.Response(200, text=PAGE))
    d = runs.run_dir(run_id)
    bad = "https://☃.net/jobs/1"
    runs.write_jsonl(d / "candidates.jsonl", [
        _cand("https://boards.greenhouse.io/acme/jobs/1"),
        CandidateUrl(query_id="web:1", query="", source_url=bad, normalized_url=bad, domain="☃.net",
                     source_type="web_search", source_key="web_search"),
    ])
    runs.mark_stage(run_id, "discover_ats")
    res = fetch(run_id)
    assert res["fetched"] == 1 and res["failed"] == 1
    assert len(runs.read_jsonl(d / "postings.jsonl")) == 1
    assert runs.read_jsonl(d / "fetch_errors.jsonl")[0]["error"] == "url_rejected: invalid_url"
