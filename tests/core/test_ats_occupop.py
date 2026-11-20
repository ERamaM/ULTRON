"""Occupop: an employer's embeddable job list (``https://api.occupop.com/api/jobs-frame/<token>``) on one page.

Synthetic fixtures only: the markup mirrors the embed's table rows and the posting JSON mirrors the fields the
vendor's public job endpoint returns (plus application-form data that must never be kept).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from ultron import http_factory, runs
from ultron.core.ats.board_policy import board_is_valid, vendor_domains
from ultron.core.ats.clients import FETCHERS, AtsClientError, fetch_vendor_jobs
from ultron.core.ats.clients.occupop import MAX_POSTINGS, occupop_job_from_detail, occupop_job_ref
from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, infer_vendor_from_host
from ultron.core.decision.unit import load_allowed_domains
from ultron.core.models import CandidateUrl
from ultron.net import HttpClient, HttpError, URLRejected
from ultron.seed_rounds import validate_proposal
from ultron.seeds import PROBE_VENDORS, Proposal, verify_proposal
from ultron.stages import fetch as fetch_stage
from ultron.stages.discover import _seed_request_host

TOKEN = "SyntheticTok3nAbCdEf0123456789XyZ"
FRAME_URL = f"https://api.occupop.com/api/jobs-frame/{TOKEN}"


class FakeClient:
    def __init__(self, text_value=""):
        self.text_value = text_value
        self.urls: list[str] = []

    def request_json(self, url, method="GET", json_body=None, timeout=None):  # pragma: no cover - never called
        raise AssertionError("Occupop boards are HTML pages")

    def request_text(self, url, timeout=None):
        self.urls.append(url)
        return self.text_value


def _row(href: str, title: str, location: str) -> str:
    return f"""
<tr class=" "><td><h4 class="title">
  <a href="{href}" target="_blank">{title}</a><br>
  <small class="location"><i class="fa fa-map-marker"></i> {location}</small>
  <small class="category"><i class="fa fa-tag"></i> Medical &amp; Healthcare</small>
  <small class="type d-none d-sm-inline"><i class="fa fa-clock-o"></i> Permanent</small>
</h4></td></tr>"""


def _frame(rows: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>Example Care</title></head><body>
<div class="table-responsive"><table class="table"><tbody class="searchable">{rows}</tbody></table></div>
<a href="https://cezannehr.com/">Powered by</a></body></html>"""


FRAME = _frame(
    _row("https://api.occupop.com/shared/job/staff-nurse-ab12c", "Staff Nurse", "Exampletown, Co. Example, Ireland")
    + _row("https://api.occupop.com/shared/job/clinical-nurse-manager-Ef34g", "Clinical Nurse Manager",
           "Sampleville, Ireland")
    + _row("https://api.occupop.com/shared/job/staff-nurse-ab12c?utm_source=x", "Staff Nurse", "Exampletown")
    + _row("https://evil.example.test/shared/job/phish-1", "Off Vendor", "Nowhere")
    + _row("https://api.occupop.com/api/integration/job/x", "Not A Posting Link", "Nowhere")
)


def fetch(board: str, client: FakeClient):
    return fetch_vendor_jobs("occupop", board, client=client, timeout=5)


def test_occupop_is_a_registered_vendor():
    assert "occupop" in FETCHERS
    assert vendor_domains("occupop") == ("occupop.com",)
    assert infer_vendor_from_host("api.occupop.com") == "occupop"
    assert http_factory.vendor_for_host("api.occupop.com") == "occupop"
    assert {"occupop.com"} <= set(load_allowed_domains())
    assert validate_proposal({"company": "Example Care", "ats": "occupop", "board": TOKEN}) == []


def test_occupop_board_makes_one_request_and_parses_rows():
    client = FakeClient(FRAME)

    postings = fetch(TOKEN, client)

    assert client.urls == [FRAME_URL]
    assert [(p.title, p.apply_url, p.location) for p in postings] == [
        ("Staff Nurse", "https://api.occupop.com/shared/job/staff-nurse-ab12c", "Exampletown, Co. Example, Ireland"),
        ("Clinical Nurse Manager", "https://api.occupop.com/shared/job/clinical-nurse-manager-Ef34g",
         "Sampleville, Ireland"),
    ]


def test_occupop_board_with_no_openings_is_empty_not_an_error():
    assert fetch(TOKEN, FakeClient(_frame(""))) == []


def test_occupop_page_without_job_table_is_an_error():
    with pytest.raises(AtsClientError, match="unparseable_board"):
        fetch(TOKEN, FakeClient("<html><body><h1>Hire the best people</h1></body></html>"))


def test_occupop_bounds_the_number_of_postings():
    rows = "".join(
        _row(f"https://api.occupop.com/shared/job/nurse-{i}-ab{i}", f"Nurse {i}", "Ireland")
        for i in range(MAX_POSTINGS + 25)
    )
    assert len(fetch(TOKEN, FakeClient(_frame(rows)))) == MAX_POSTINGS


@pytest.mark.parametrize("board", ["short", "a/b", "..", "https://x.test", "", "tok-en-with-dashes", TOKEN + "!"])
def test_occupop_rejects_invalid_board_before_any_request(board):
    client = FakeClient(FRAME)
    with pytest.raises(AtsClientError, match="invalid_board"):
        fetch(board, client)
    assert client.urls == []


@pytest.mark.parametrize(
    ("url", "board"),
    [
        (FRAME_URL, TOKEN),
        (FRAME_URL + "?&search=1", TOKEN),
        ("https://api.occupop.com/shared/job/staff-nurse-ab12c", None),  # a posting names no board
        ("https://www.occupop.com/", None),
        ("https://occupop.com/pricing", None),
        ("https://app.occupop.com/login", None),
    ],
)
def test_occupop_seed_from_careers_url_keeps_the_token_case(url, board):
    seed = extract_seed_from_url(url, "Example Care")
    if board is None:
        assert seed is None
    else:
        assert seed is not None and seed.ats == "occupop" and seed.board_identifier == board
        assert seed.company == "Example Care"  # an opaque token never names the company
        assert board_is_valid("occupop", seed.board_identifier)


def test_occupop_token_case_survives_verification_and_discovery():
    # Share tokens are case-sensitive: the stored board and the board request keep the case of the embed URL.
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.occupop.com" and request.url.path == f"/api/jobs-frame/{TOKEN}":
            return httpx.Response(200, text=FRAME, headers={"content-type": "text/html"})
        return httpx.Response(404)

    res = verify_proposal(Proposal("Example Care", careers_url=FRAME_URL), client=_http_client(handler), timeout=5)

    assert res.status == "verified" and res.seed.ats == "occupop" and res.seed.board_identifier == TOKEN
    assert seen == [FRAME_URL]
    assert seed_identity("occupop", TOKEN) == ("occupop", TOKEN.lower())  # identity folds; the board does not
    fetch_vendor_jobs("occupop", res.seed.board_identifier, client=_http_client(handler), timeout=5)
    assert seen[-1] == FRAME_URL


def test_occupop_is_not_guessed_by_slug():
    assert "occupop" not in PROBE_VENDORS


def test_occupop_seeds_count_against_the_api_host():
    assert _seed_request_host(SimpleNamespace(ats="occupop", board_identifier=TOKEN)) == "api.occupop.com"


@pytest.mark.parametrize(
    ("url", "ref"),
    [
        ("https://api.occupop.com/shared/job/staff-nurse-ab12c", "staff-nurse-ab12c"),
        ("https://api.occupop.com/shared/job/staff-nurse-kfgK/", "staff-nurse-kfgK"),
        ("https://api.occupop.com/shared/job/../../api/x", ""),
        ("https://evil.example.test/shared/job/staff-nurse-ab12c", ""),
        ("https://api.occupop.com.evil.test/shared/job/staff-nurse-ab12c", ""),
        ("https://api.occupop.com/api/jobs-frame/" + TOKEN, ""),
    ],
)
def test_occupop_job_ref_accepts_only_vendor_posting_links(url, ref):
    assert occupop_job_ref(url) == ref


def _detail(**job) -> dict:
    base = {
        "title": "Staff Nurse", "closed": False, "status": True, "is_active": True, "workplace": "onSite",
        "description": "<p>Registered nurse (NMBI) for our <strong>residential</strong> service.</p>",
        "city_name": "Exampletown, Co. Example, X00 Y000", "country": "Ireland", "company_name": "EXAMPLE CARE",
        "created_at": "2026-11-06T09:28:58.000000Z",
        "questions_for_applicants": {"Default": [{"text": "Synthetic screening question?"}]},
        "file_upload_signed_url": "https://uploads.example.test/signed?X-Amz-Signature=synthetic",
    }
    base.update(job)
    return {"job": base}


def test_occupop_detail_maps_the_posting_and_drops_form_data():
    url = "https://api.occupop.com/shared/job/staff-nurse-ab12c"
    job = occupop_job_from_detail(_detail(), url)

    assert job is not None
    assert (job.title, job.company, job.apply_url, job.domain) == ("Staff Nurse", "EXAMPLE CARE", url, "api.occupop.com")
    assert job.location == "Exampletown, Co. Example, X00 Y000, Ireland"
    assert job.remote_text == "On-site | Exampletown, Co. Example, X00 Y000, Ireland"
    assert "NMBI" in job.body_text and "residential" in job.body_text
    dumped = json.dumps(job.model_dump())
    assert "signed" not in dumped and "screening" not in dumped


@pytest.mark.parametrize("payload", [{"job": None}, _detail(closed=True), _detail(is_active=False), _detail(title=""),
                                     [], "x"])
def test_occupop_detail_of_a_closed_or_missing_job_is_none(payload):
    assert occupop_job_from_detail(payload, "https://api.occupop.com/shared/job/x-1") is None


def _http_client(handler) -> HttpClient:
    return HttpClient(
        max_requests=20, max_requests_per_host=20, min_interval_per_host=0.0, transport=httpx.MockTransport(handler),
        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None,
    )


def test_occupop_board_redirecting_off_vendor_is_not_followed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example.test/"})

    with pytest.raises((HttpError, URLRejected)):
        fetch_vendor_jobs("occupop", TOKEN, client=_http_client(handler), timeout=5)
    assert seen == ["api.occupop.com"]


def test_run_client_paces_occupop_requests():
    assert http_factory.VENDOR_MIN_INTERVALS.get("occupop", 0) >= 3.0
    clock = {"now": 0.0, "slept": []}

    def sleep(seconds):
        clock["slept"].append(seconds)
        clock["now"] += seconds

    run = {"settings": {"max_pages": 10, "max_seeds_per_run": 10, "max_requests_per_host": 5, "ats_api_timeout": 5,
                        "max_requests_per_vendor": 50}}
    client = http_factory.client_for_run(
        run, transport=httpx.MockTransport(lambda r: httpx.Response(200, text=FRAME)),
        resolver=lambda h, p: ["93.184.216.34"], sleep=sleep, clock=lambda: clock["now"], min_interval_per_host=0.0,
    )
    for _ in range(3):
        fetch_vendor_jobs("occupop", TOKEN, client=client, timeout=5)
    assert clock["slept"] == [http_factory.VENDOR_MIN_INTERVALS["occupop"]] * 2


def _fetch_run(tmp_path, monkeypatch, handler, url: str) -> tuple[list[dict], list[dict], list[str]]:
    """Run the fetch stage on one Occupop candidate against a mock transport."""
    monkeypatch.setenv("ULTRON_HOME", str(tmp_path / "home"))
    seen: list[str] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return handler(request)

    run = {"settings": {"max_pages": 5, "max_seeds_per_run": 5, "max_requests_per_host": 5, "ats_api_timeout": 5,
                        "max_requests_per_vendor": 50}}
    monkeypatch.setattr(fetch_stage, "client_for_run", lambda run, **kw: http_factory.client_for_run(
        run, transport=httpx.MockTransport(recording), resolver=lambda h, p: ["93.184.216.34"],
        sleep=lambda s: None, min_interval_per_host=0.0, **kw))
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    candidate = CandidateUrl(
        source_url=url, normalized_url=url, domain="api.occupop.com", source_type="ats_api",
        source_key=f"occupop:{TOKEN}", query_id="ats-occupop", query="Example Care [occupop]",
        ats_title="Staff Nurse", ats_company="Example Care",
        ats_location="Exampletown, Ireland",
    )
    runs.write_jsonl(out_dir / "candidates.jsonl", [candidate.model_dump(mode="json")])
    monkeypatch.setattr(fetch_stage.runs, "require_stage", lambda run_id, stage: None)
    monkeypatch.setattr(fetch_stage.runs, "load_run", lambda run_id: run)
    monkeypatch.setattr(fetch_stage.runs, "run_dir", lambda run_id: out_dir)
    monkeypatch.setattr(fetch_stage.runs, "mark_stage", lambda run_id, stage: None)
    fetch_stage.fetch("synthetic-run")
    return runs.read_jsonl(out_dir / "postings.jsonl"), runs.read_jsonl(out_dir / "fetch_errors.jsonl"), seen


def test_fetch_reads_occupop_postings_from_the_job_json(tmp_path, monkeypatch):
    # The posting link redirects (302) to a script-only page on another host; fetch must read the vendor's JSON
    # instead, keep the Occupop link as the apply URL, and never open the redirect target.
    url = "https://api.occupop.com/shared/job/staff-nurse-ab12c"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/integration/job/staff-nurse-ab12c":
            return httpx.Response(200, json=_detail(), headers={"content-type": "application/json"})
        if request.url.path.startswith("/shared/job/"):
            return httpx.Response(302, headers={"location": "https://spa.example.test/shared/job/staff-nurse-ab12c"})
        return httpx.Response(200, text="<html><body><div id='app'></div></body></html>")

    postings, errors, seen = _fetch_run(tmp_path, monkeypatch, handler, url)

    assert errors == []
    assert seen == ["https://api.occupop.com/api/integration/job/staff-nurse-ab12c"]
    assert len(postings) == 1
    job = postings[0]["job"]
    assert (job["title"], job["apply_url"], job["company"]) == ("Staff Nurse", url, "EXAMPLE CARE")
    assert "NMBI" in job["body_text"]


def test_fetch_marks_a_missing_occupop_job_closed(tmp_path, monkeypatch):
    url = "https://api.occupop.com/shared/job/staff-nurse-ab12c"
    postings, errors, _seen = _fetch_run(
        tmp_path, monkeypatch, lambda r: httpx.Response(200, json={"job": None}), url,
    )
    assert postings == []
    assert [e["error"] for e in errors] == ["expired_or_closed"]


def test_fetch_does_not_follow_an_occupop_json_redirect_off_vendor(tmp_path, monkeypatch):
    url = "https://api.occupop.com/shared/job/staff-nurse-ab12c"
    postings, errors, seen = _fetch_run(
        tmp_path, monkeypatch, lambda r: httpx.Response(302, headers={"location": "https://evil.example.test/x"}), url,
    )
    assert postings == []
    assert len(errors) == 1
    assert all("evil.example.test" not in u for u in seen)


def test_verify_finds_the_occupop_embed_on_a_company_careers_page():
    seen: list[str] = []
    careers = f'<html><body><iframe src="{FRAME_URL}?&search=1" width="100%"></iframe></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "examplecare.example.test":
            return httpx.Response(200, text=careers, headers={"content-type": "text/html"})
        if request.url.host == "api.occupop.com" and request.url.path == f"/api/jobs-frame/{TOKEN}":
            return httpx.Response(200, text=FRAME, headers={"content-type": "text/html"})
        return httpx.Response(404)

    res = verify_proposal(
        Proposal("Example Care", domain="examplecare.example.test",
                 careers_url="https://examplecare.example.test/careers"),
        client=_http_client(handler), timeout=5,
    )

    assert res.status == "verified" and res.seed.ats == "occupop" and res.seed.board_identifier == TOKEN
    assert seen == ["https://examplecare.example.test/careers", FRAME_URL]
