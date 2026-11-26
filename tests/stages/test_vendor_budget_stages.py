"""The vendor budget and breaker in discover, fetch and `seeds verify` (self-improvement session 4).

Session 3 queried 18 Workday tenants in one balanced run (about 1,100 requests to ``*.myworkdayjobs.com``) and the
vendor answered 429. Per-host caps never bounded the vendor as a whole.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from ultron import seeds
from ultron.core.models import CandidateUrl
from ultron.profiles import store as profiles
from ultron.seeds import rounds as seed_rounds
from ultron.stages import fetch as fetch_stage
from ultron.stages import http_factory
from ultron.stages.discover import discover_ats
from ultron.storage import runs

PAGE = (Path(__file__).resolve().parents[1] / "fixtures" / "pages" / "greenhouse_job.html").read_text(encoding="utf-8")


@pytest.fixture
def pack_dir(tmp_path, monkeypatch):
    path = tmp_path / "packs"
    path.mkdir()
    monkeypatch.setattr(seeds.inventory, "packs_dir", lambda: path)
    profiles.install_example()
    return path


def _workday_pack(pack_dir: Path, count: int) -> list[str]:
    hosts = [f"tenant{i}.wd{1 + i % 5}.myworkdayjobs.com" for i in range(count)]
    rows = [{"company": f"Tenant {i}", "ats": "workday", "board_identifier": f"{host}/External",
             "priority": 10 + i, "sectors": ["technology"]} for i, host in enumerate(hosts)]
    (pack_dir / "wd.yaml").write_text(yaml.safe_dump({"pack": {"id": "wd", "usage": "active"}, "seeds": rows}),
                                      encoding="utf-8")
    return hosts


def _overrides(handler) -> None:
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0,
    )


def _jobs(offset: int, total: int, prefix: str = "Senior ML Engineer") -> dict:
    count = max(0, min(20, total - offset))
    return {"total": total if offset == 0 else 0, "jobPostings": [
        {"title": f"{prefix} {offset + i}", "externalPath": f"/job/Remote-EU/ML_R-{offset + i}",
         "locationsText": "Remote EU"} for i in range(count)]}


def test_two_429_responses_skip_every_other_workday_board(pack_dir):
    hosts = _workday_pack(pack_dir, 8)
    hits: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        hits[request.url.host] = hits.get(request.url.host, 0) + 1
        return httpx.Response(429)

    _overrides(handler)
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced", {"max_seeds_per_run": 20})
    result = discover_ats(run_id)
    assert list(hits.values()) == [2]
    errors = runs.read_jsonl(runs.run_dir(run_id) / "seed_errors.jsonl")
    vendor_rows = [e for e in errors if e.get("error") == "vendor_blocked"]
    # The first board trips the breaker during its retry, so its HTML fallback is also blocked.
    assert len(vendor_rows) == len(hosts) and all(e.get("vendor") == "workday" for e in vendor_rows)
    assert result["vendors_blocked"] == ["workday"]

    # The breaker holds for the rest of the run: fetch sends nothing to any Workday tenant.
    url = f"https://{hosts[5]}/External/job/Remote-EU/ML_R-1"
    runs.write_jsonl(runs.run_dir(run_id) / "candidates.jsonl", [CandidateUrl(
        query_id="ats:x", query="", source_url=url, normalized_url=url, domain=hosts[5],
        source_type="ats_api", source_key="ats:workday:x").model_dump(mode="json")])
    before = sum(hits.values())
    fetch_stage.fetch(run_id)
    assert sum(hits.values()) == before
    assert runs.read_jsonl(runs.run_dir(run_id) / "fetch_errors.jsonl")[0]["error"] == "vendor_blocked"


def test_discover_shares_the_vendor_allowance_between_workday_tenants(pack_dir):
    _workday_pack(pack_dir, 4)
    pages: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        pages[request.url.host] = pages.get(request.url.host, 0) + 1
        return httpx.Response(200, json=_jobs(json.loads(request.content)["offset"], 400))

    _overrides(handler)
    # 40 vendor requests per run, discover may use half: 20 pages over 4 tenants, 5 each.
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced", {"max_requests_per_vendor": 40})
    result = discover_ats(run_id)
    assert sorted(pages.values()) == [5, 5, 5, 5]
    assert result["requests_by_vendor"] == {"workday": 20}
    assert result["seeds_failed"] == 0 and result["candidates"] > 0


def test_workday_keeps_collected_postings_when_the_vendor_budget_runs_out_mid_board():
    from ultron.core.ats.clients.workday import fetch_workday_jobs
    from ultron.infra.net import RequestBudgetExceeded

    class TwoPages:
        calls = 0

        def request_json(self, url, method="GET", json_body=None, timeout=None):
            self.calls += 1
            if self.calls > 2:
                raise RequestBudgetExceeded("vendor", scope="vendor", host="acme.wd1.myworkdayjobs.com",
                                            vendor="workday")
            return _jobs(json_body["offset"], 400)

        def request_text(self, url, timeout=None):
            raise AssertionError("no HTML fallback when postings were collected")

    jobs = fetch_workday_jobs("acme.wd1.myworkdayjobs.com/External", client=TwoPages())
    assert len(jobs) == 40


def test_fetch_skips_the_vendor_once_its_run_budget_is_spent(pack_dir):
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return httpx.Response(200, text=PAGE)

    _overrides(handler)
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced", {"max_requests_per_vendor": 3})
    cands = []
    for i in range(6):
        host = f"tenant{i}.wd1.myworkdayjobs.com"
        url = f"https://{host}/External/job/Remote-EU/ML_R-{i}"
        cands.append(CandidateUrl(query_id="ats:x", query="", source_url=url, normalized_url=url, domain=host,
                                  source_type="ats_api", source_key=f"ats:workday:{i}").model_dump(mode="json"))
    runs.write_jsonl(runs.run_dir(run_id) / "candidates.jsonl", cands)
    runs.mark_stage(run_id, "discover_ats")
    result = fetch_stage.fetch(run_id)
    assert len(hits) == 3  # page fetches and enrichment calls share the vendor's 3 requests
    errors = [e["error"] for e in runs.read_jsonl(runs.run_dir(run_id) / "fetch_errors.jsonl")]
    assert errors and all(e.startswith(("vendor_budget_exhausted", "skipped_vendor_budget_exhausted")) for e in errors)
    assert result["requests_by_vendor"] == {"workday": 3}


def test_seeds_verify_reports_a_spent_vendor_budget_as_unchecked():
    calls = {"n": 0}

    class Spent:
        def request_json(self, *args, **kwargs):
            calls["n"] += 1
            from ultron.infra.net import RequestBudgetExceeded
            raise RequestBudgetExceeded("vendor budget", scope="vendor", host="acme.wd1.myworkdayjobs.com",
                                        vendor="workday")

        request_text = request_json

    result = seeds.verify_proposal(
        seeds.Proposal(company="Acme", ats="workday", board="acme.wd1.myworkdayjobs.com/External"),
        client=Spent(), timeout=5, verify_only=True,
    )
    assert result.reason == "vendor_budget_exhausted"
    assert "vendor_budget_exhausted" in seed_rounds._UNCHECKED


def test_postings_skipped_by_the_vendor_budget_leave_their_page_slot_to_capped_candidates(pack_dir):
    # Session 4, S6 balanced: 222 Workday postings skipped by the vendor cap (no request) still used page slots,
    # while 31 postings of other vendors were left out by max_pages.
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.host)
        return httpx.Response(200, text=PAGE)

    _overrides(handler)
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced", {"max_requests_per_vendor": 1, "max_pages": 3})
    cands = []
    for i in range(4):  # four lanes listed first: they lead the round robin
        host = f"tenant{i}.wd1.myworkdayjobs.com"
        url = f"https://{host}/External/job/Remote-EU/ML_R-{i}"
        cands.append(CandidateUrl(query_id="ats:x", query="", source_url=url, normalized_url=url, domain=host,
                                  source_type="ats_api", source_key=f"ats:workday:{i}").model_dump(mode="json"))
    url = "https://employer.example.org/jobs/1"
    cands.append(CandidateUrl(query_id="web:1", query="", source_url=url, normalized_url=url,
                              domain="employer.example.org", source_type="web_search",
                              source_key="web:employer").model_dump(mode="json"))
    runs.write_jsonl(runs.run_dir(run_id) / "candidates.jsonl", cands)
    runs.mark_stage(run_id, "discover_ats")
    result = fetch_stage.fetch(run_id)
    assert any(host == "employer.example.org" for host in hits)
    assert runs.read_jsonl(runs.run_dir(run_id) / "fetch_capped.jsonl") == []
    assert result["capped"] == 0


def test_a_large_workday_share_never_exceeds_the_client_safety_cap():
    from ultron.core.ats.clients.workday import fetch_workday_jobs

    class Endless:
        calls = 0

        def request_json(self, url, method="GET", json_body=None, timeout=None):
            self.calls += 1
            return _jobs(json_body["offset"], 100_000)

        def request_text(self, url, timeout=None):
            raise AssertionError("not reached")

    client = Endless()
    fetch_workday_jobs("acme.wd1.myworkdayjobs.com/External", client=client, max_pages=500)
    assert client.calls == 100


def _blocked_run(vendor: str = "workday") -> str:
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    (runs.run_dir(run_id) / http_factory.VENDOR_STATE_FILE).write_text(
        json.dumps({"requests": {}, "rate_limited": {vendor: 2}, "forbidden_streak": {}, "blocked": [vendor]}),
        encoding="utf-8")
    return run_id


def test_skips_without_a_request_do_not_dilute_the_navigation_failure_stop(pack_dir):
    # Codex review: 100 vendor-blocked skips + 60 connection failures counted 160 attempts, so fetch never stopped.
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    _overrides(handler)
    run_id = _blocked_run()
    cands = []
    for i in range(100):
        host = f"tenant{i}.wd1.myworkdayjobs.com"
        url = f"https://{host}/External/job/Remote-EU/ML_R-{i}"
        cands.append(CandidateUrl(query_id="ats:x", query="", source_url=url, normalized_url=url, domain=host,
                                  source_type="ats_api", source_key=f"ats:workday:{i}").model_dump(mode="json"))
    for i in range(80):
        url = f"https://down{i}.example.org/jobs/{i}"
        cands.append(CandidateUrl(query_id="web:1", query="", source_url=url, normalized_url=url,
                                  domain=f"down{i}.example.org", source_type="web_search",
                                  source_key=f"web:down{i}").model_dump(mode="json"))
    runs.write_jsonl(runs.run_dir(run_id) / "candidates.jsonl", cands)
    runs.mark_stage(run_id, "discover_ats")
    result = fetch_stage.fetch(run_id)
    assert result.get("stopped") == "navigation_failures"


@pytest.mark.parametrize(("vendor", "board"), [
    ("icims", "careers-acme.icims.com"),
    ("taleo", "acme.taleo.net/careersection/ex/jobsearch.ftl"),
])
def test_a_blocked_vendor_is_not_reported_as_a_checked_board(pack_dir, vendor, board):
    # Codex review: iCIMS and Taleo turned VendorBlocked into AtsClientError("browser_fallback_unavailable"), so
    # `seeds verify` said no_board_found (a checked company) instead of host_blocked.
    from ultron.core.ats.clients import fetch_vendor_jobs
    from ultron.infra.net import VendorBlocked

    _overrides(lambda request: httpx.Response(500))
    run_id = _blocked_run(vendor)
    with pytest.raises(VendorBlocked):
        fetch_vendor_jobs(vendor, board, client=http_factory.client_for_run(runs.load_run(run_id)), timeout=5)
    result = seeds.verify_proposal(seeds.Proposal(company="Acme", ats=vendor, board=board),
                                   client=http_factory.client_for_run(runs.load_run(run_id)), timeout=5,
                                   verify_only=True)
    assert result.reason == "host_blocked"


@pytest.mark.parametrize("vendor", ["bamboohr", "comeet", "factorial", "greenhouse", "jobvite", "lever", "personio",
                                    "pinpoint", "workday"])
def test_every_client_with_a_fallback_propagates_a_blocked_vendor(vendor):
    from ultron.core.ats.clients import fetch_vendor_jobs
    from ultron.infra.net import VendorBlocked

    calls = {"n": 0}

    class Blocked:
        def request_json(self, url, *args, **kwargs):
            calls["n"] += 1
            raise VendorBlocked(vendor, "x")

        request_text = request_json

    board = {"workday": "acme.wd1.myworkdayjobs.com/External"}.get(vendor, "acme")
    with pytest.raises(VendorBlocked):
        fetch_vendor_jobs(vendor, board, client=Blocked(), timeout=5)
    assert calls["n"] == 1  # no fallback request (EU host, HTML page) after the block
