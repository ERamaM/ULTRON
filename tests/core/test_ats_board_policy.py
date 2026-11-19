"""FR-1 / spec §5.7: ATS API calls stay on the vendor's hosts; board identifiers are validated first."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from ultron import http_factory, profiles, runs, seeds
from ultron.core.ats.board_policy import board_is_valid, vendor_domains
from ultron.core.ats.clients import AtsClientError, fetch_vendor_jobs
from ultron.core.ats.vendors import SUPPORTED_ATS_NAMES
from ultron.core.models import AtsBoardSeed
from ultron.core.urlpolicy import URLRejected, domain_is_allowed
from ultron.net import HttpClient
from ultron.seeds import Proposal, verify_proposal

PACKS = Path(__file__).resolve().parents[2] / "skill" / "ultron" / "data" / "seed_packs"


class RecordingClient:
    def __init__(self):
        self.urls: list[str] = []

    def request_json(self, url, method="GET", json_body=None, timeout=None):
        self.urls.append(url)
        return {}

    def request_text(self, url, timeout=None):
        self.urls.append(url)
        return ""


def _http(handler, seen: list[str]) -> HttpClient:
    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return handler(request)

    return HttpClient(max_requests=80, max_requests_per_host=80, min_interval_per_host=0.0,
                      transport=httpx.MockTransport(recording), resolver=lambda h, p: ["93.184.216.34"],
                      sleep=lambda s: None)


def test_every_enabled_bundled_seed_board_is_valid():
    for pack in PACKS.glob("*.yaml"):
        for row in yaml.safe_load(pack.read_text(encoding="utf-8"))["seeds"]:
            valid = board_is_valid(row["ats"], str(row["board_identifier"]))
            if row.get("enabled", True):
                assert valid, (pack.name, row)
            elif not valid:  # disabled placeholders: a bare name for a host-style vendor, never queryable
                assert row["ats"] in {"workday", "icims", "taleo"} and "." not in str(row["board_identifier"])


@pytest.mark.parametrize(("vendor", "board"), [
    ("greenhouse", "acme"), ("lever", "acme-labs"), ("smartrecruiters", "BoschGroup"), ("personio", "acme_gmbh"),
    ("workday", "acme.wd5.myworkdayjobs.com/External"), ("workday", "acme.wd3.myworkdayjobs.com/en-US/Careers/x"),
    ("workday", "wd1.myworkdaysite.com/recruiting/acme/External"), ("icims", "careers-acme.icims.com"),
    ("taleo", "acme.taleo.net"), ("taleo", "acme.taleo.net/careersection/ex"),
])
def test_normal_boards_are_valid(vendor, board):
    assert board_is_valid(vendor, board)


@pytest.mark.parametrize(("vendor", "board"), [
    ("greenhouse", "acme/../admin"), ("greenhouse", "a/b"), ("lever", "acme\\x"), ("ashby", "me@evil.example"),
    ("breezy", "evil.example#"), ("breezy", "evil.example?x="), ("recruitee", "acme corp"), ("teamtailor", "a..b"),
    ("greenhouse", "acme\n"), ("greenhouse", ""), ("workday", "evil.example/External"),
    ("workday", "https://evil.example/External"), ("workday", "acme.myworkdayjobs.com.evil.example/External"),
    ("workday", "acme.wd5.myworkdayjobs.com/../x"), ("icims", "careers.acme.com"),
    ("taleo", "acme.taleo.net/a b"), ("taleo", "user@acme.taleo.net"), ("nosuchvendor", "acme"),
])
def test_bad_boards_are_invalid(vendor, board):
    assert not board_is_valid(vendor, board)


def test_vendor_domains_admit_the_hosts_the_clients_call():
    hosts = {
        "greenhouse": "boards-api.greenhouse.io", "lever": "api.eu.lever.co", "ashby": "jobs.ashbyhq.com",
        "workable": "apply.workable.com", "comeet": "www.comeet.co", "smartrecruiters": "api.smartrecruiters.com",
        "personio": "acme.jobs.personio.de", "workday": "acme.wd5.myworkdayjobs.com", "jazzhr": "acme.applytojob.com",
    }
    for vendor, host in hosts.items():
        assert domain_is_allowed(host, vendor_domains(vendor)), vendor
    assert not domain_is_allowed("greenhouse.io.evil.example", vendor_domains("greenhouse"))
    assert not domain_is_allowed("evilgreenhouse.io", vendor_domains("greenhouse"))
    assert all(vendor_domains(name) for name in SUPPORTED_ATS_NAMES)


def test_fetch_vendor_jobs_rejects_invalid_board_before_any_request():
    client = RecordingClient()
    with pytest.raises(AtsClientError, match="invalid_board"):
        fetch_vendor_jobs("workday", "evil.example/External", client=client, timeout=5)
    assert client.urls == []


def test_vendor_call_cannot_be_redirected_off_the_vendor_hosts():
    seen: list[str] = []

    def handler(request):
        if request.url.host == "boards-api.greenhouse.io":
            return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
        return httpx.Response(200, json={"jobs": []})

    with pytest.raises(URLRejected, match="domain_not_allowed"):
        fetch_vendor_jobs("greenhouse", "acme", client=_http(handler, seen), timeout=5)
    assert "evil.example" not in seen


def test_shared_client_is_not_restricted_outside_vendor_calls():
    seen: list[str] = []
    client = _http(lambda r: httpx.Response(200, text="ok"), seen)
    assert client.request_text("https://careers.acme.example/") == "ok"


def test_verify_proposal_reports_invalid_board_without_contacting_it():
    seen: list[str] = []
    client = _http(lambda r: httpx.Response(404), seen)
    res = verify_proposal(Proposal("Acme", ats="workday", board="evil.example/External"), client=client, timeout=5)
    assert res.status == "unresolved" and res.reason == "invalid_board"
    assert "evil.example" not in seen
    assert not seen  # explicit ats+board does not fall back to slug probes


def test_verify_proposal_still_verifies_a_real_board_through_the_vendor_scope():
    seen: list[str] = []

    def handler(request):
        if request.url.host == "boards-api.greenhouse.io" and "/acme/" in request.url.path:
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(404)

    res = verify_proposal(Proposal("Acme", ats="greenhouse", board="acme"), client=_http(handler, seen), timeout=5)
    assert res.status == "verified" and seen == ["boards-api.greenhouse.io"]


def test_discover_records_invalid_board_and_never_requests_it(tmp_path, monkeypatch):
    from ultron.stages.discover import discover_ats

    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: empty)
    seen: list[str] = []

    def handler(request):
        seen.append(request.url.host)
        return httpx.Response(404)

    profiles.install_example()
    bad = AtsBoardSeed(seed_id="workday:evil.example/External", company="Evil", ats="workday",
                       board_identifier="evil.example/External", profiles=[profiles.EXAMPLE_SLUG])
    seeds.add_seeds([bad], profile_slug=profiles.EXAMPLE_SLUG)
    http_factory.set_test_overrides(transport=httpx.MockTransport(handler), resolver=lambda h, p: ["93.184.216.34"],
                                    sleep=lambda s: None, min_interval_per_host=0.0)
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    discover_ats(run_id)
    errors = runs.read_jsonl(runs.run_dir(run_id) / "seed_errors.jsonl")
    assert {"seed_id": "workday:evil.example/external", "error": "invalid_board"} in [
        {"seed_id": e["seed_id"].lower(), "error": e["error"]} for e in errors
    ]
    assert "evil.example" not in seen
    assert json.dumps(errors)  # stays serialisable
