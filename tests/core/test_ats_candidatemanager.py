"""CandidateManager: an employer's vacancy list (``www.candidatemanager.net/cm/p/pJobs.aspx?mid=..&sid=..``).

Synthetic fixtures only: the markup mirrors the ASP.NET vacancy table and detail page (the employer-themed site
around them is reduced to a navigation bar, a location filter and a footer).
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from ultron.core.ats.board_policy import board_is_valid, vendor_domains
from ultron.core.ats.clients import FETCHERS, AtsClientError, fetch_vendor_jobs
from ultron.core.ats.clients.candidatemanager import MAX_POSTINGS
from ultron.core.ats.registry import extract_seed_from_url, infer_vendor_from_host, parse_ats_page
from ultron.core.decision.unit import load_allowed_domains
from ultron.core.discovery.url_filter import normalize_url
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import JobRawPage
from ultron.infra.net import HttpClient, HttpError, URLRejected
from ultron.seeds import PROBE_VENDORS, Proposal, verify_proposal
from ultron.seeds.rounds import validate_proposal
from ultron.stages import http_factory
from ultron.stages.discover import _seed_request_host

BOARD = "QwErT-ZxCv"
BOARD_URL = "https://www.candidatemanager.net/cm/p/pJobs.aspx?mid=QwErT&sid=ZxCv"
DETAIL = "https://www.candidatemanager.net/cm/p/pJobDetails.aspx?mid=QwErT&sid=ZxCv&jid={jid}"


class FakeClient:
    def __init__(self, text_value=""):
        self.text_value = text_value
        self.urls: list[str] = []

    def request_json(self, url, method="GET", json_body=None, timeout=None):  # pragma: no cover - never called
        raise AssertionError("CandidateManager boards are HTML pages")

    def request_text(self, url, timeout=None):
        self.urls.append(url)
        return self.text_value


def _row(href: str, title: str, location: str) -> str:
    return f"""
<tr><td>
  <a href="{href}">
      {title}
  </a>
  <small>(2026-101)</small>
</td><td>Nursing</td><td><div title="{location}">{location}</div></td></tr>"""


def _board(rows: str) -> str:
    return f"""<!DOCTYPE html><html><head><title>Candidate Manager - Job Vacancies</title></head><body>
<nav><a href="https://employer.example.test/">Home</a></nav>
<form name="aspnetForm" method="post" action="./pJobs.aspx?mid=QwErT&amp;sid=ZxCv" id="aspnetForm">
<select><option url="/cm/p/pJobs.aspx?mid=QwErT&amp;sid=ZxCv&amp;jloc=Exampletown">Exampletown (2)</option></select>
<table class="table table-striped"><tr><th>Current Vacancies</th><th>Category</th><th>Location</th></tr>
{rows}
</table></form></body></html>"""


BOARD_HTML = _board(
    _row(DETAIL.format(jid="ABCDE") + "&a=t0KeN%253d", "Staff Nurse", "Exampletown, County Example, Ireland")
    + _row("pJobDetails.aspx?mid=QwErT&sid=ZxCv&jid=FGHIJ&a=t0KeN%253d", "Clinical Nurse Manager",
           "Sampleville, County Example, Ireland")
    + _row(DETAIL.format(jid="ABCDE"), "Staff Nurse", "Exampletown, County Example, Ireland")
    + _row("https://www.candidatemanager.net/cm/p/pJobDetails.aspx?mid=OTHER&sid=ZxCv&jid=KLMNO", "Other Board", "X")
    + _row("https://evil.example.test/cm/p/pJobDetails.aspx?mid=QwErT&sid=ZxCv&jid=PQRST", "Off Vendor", "Nowhere")
)


def fetch(board: str, client: FakeClient):
    return fetch_vendor_jobs("candidatemanager", board, client=client, timeout=5)


def test_candidatemanager_is_a_registered_vendor():
    assert "candidatemanager" in FETCHERS
    assert vendor_domains("candidatemanager") == ("candidatemanager.net",)
    assert infer_vendor_from_host("www.candidatemanager.net") == "candidatemanager"
    assert http_factory.vendor_for_host("www.candidatemanager.net") == "candidatemanager"
    assert {"candidatemanager.net"} <= set(load_allowed_domains())
    assert validate_proposal({"company": "Example Care", "ats": "candidatemanager", "board": BOARD}) == []


def test_candidatemanager_board_makes_one_request_and_parses_rows():
    client = FakeClient(BOARD_HTML)

    postings = fetch(BOARD, client)

    assert client.urls == [BOARD_URL]
    assert [(p.title, p.apply_url, p.location) for p in postings] == [
        ("Staff Nurse", DETAIL.format(jid="ABCDE"), "Exampletown, County Example, Ireland"),
        ("Clinical Nurse Manager", DETAIL.format(jid="FGHIJ"), "Sampleville, County Example, Ireland"),
    ]


def test_candidatemanager_board_with_no_vacancies_is_empty_not_an_error():
    assert fetch(BOARD, FakeClient(_board(""))) == []


def test_candidatemanager_page_without_vacancy_form_is_an_error():
    with pytest.raises(AtsClientError, match="unparseable_board"):
        fetch(BOARD, FakeClient("<html><body><h1>Applicant tracking software</h1></body></html>"))


def test_candidatemanager_bounds_the_number_of_postings():
    rows = "".join(_row(DETAIL.format(jid=f"J{i}"), f"Nurse {i}", "Ireland") for i in range(MAX_POSTINGS + 25))
    assert len(fetch(BOARD, FakeClient(_board(rows)))) == MAX_POSTINGS


@pytest.mark.parametrize(
    "board", ["QwErT", "QwErT-", "-ZxCv", "Qw.ErT-ZxCv", "QwErT-Zx-Cv", "a/b-c", "..", "*syu~op-qmkp", ""],
)
def test_candidatemanager_rejects_invalid_board_before_any_request(board):
    client = FakeClient(BOARD_HTML)
    with pytest.raises(AtsClientError, match="invalid_board"):
        fetch(board, client)
    assert client.urls == []


@pytest.mark.parametrize(
    ("url", "board"),
    [
        (BOARD_URL, BOARD),
        (BOARD_URL + "&Site=Hospital", BOARD),
        (DETAIL.format(jid="ABCDE") + "&a=t0KeN%253d", BOARD),
        ("https://www.candidatemanager.net/cm/p/pJobs.aspx?mid=%2Asyu%7Eop&sid=qmkp", None),
        ("https://www.candidatemanager.net/", None),
        ("https://www.candidatemanager.net/terms-and-conditions/", None),
        ("https://candidatemanager.net/applicant-tracking/", None),
    ],
)
def test_candidatemanager_seed_from_careers_url_keeps_the_case(url, board):
    seed = extract_seed_from_url(url, "Example Care")
    if board is None:
        assert seed is None
    else:
        assert seed is not None and seed.ats == "candidatemanager" and seed.board_identifier == board
        assert seed.company == "Example Care"
        assert board_is_valid("candidatemanager", seed.board_identifier)


def test_candidatemanager_is_not_guessed_by_slug():
    assert "candidatemanager" not in PROBE_VENDORS


def test_candidatemanager_seeds_count_against_the_vendor_host():
    seed = SimpleNamespace(ats="candidatemanager", board_identifier=BOARD)
    assert _seed_request_host(seed) == "www.candidatemanager.net"


def test_candidatemanager_posting_identity_ignores_the_extra_token_and_stays_fetchable():
    plain = canonical_job_identity(DETAIL.format(jid="ABCDE"))
    tokened = canonical_job_identity(DETAIL.format(jid="ABCDE") + "&a=t0KeN%253d")
    other = canonical_job_identity(DETAIL.format(jid="FGHIJ"))
    assert plain.identity_key == tokened.identity_key == "candidatemanager:QwErT-ZxCv:ABCDE"
    assert other.identity_key != plain.identity_key
    normalized = normalize_url(DETAIL.format(jid="ABCDE"))
    assert normalized.startswith("https://www.candidatemanager.net/cm/p/pJobDetails.aspx?")
    assert {"mid=QwErT", "sid=ZxCv", "jid=ABCDE"} <= set(normalized.split("?", 1)[1].split("&"))


POSTING = """<!DOCTYPE html><html><head><title>Candidate Manager - </title></head><body>
<nav>Home | Our Homes in Farawaytown, Spain | Careers | Remote Work Policy</nav>
<form name="aspnetForm" method="post" action="./pJobDetails.aspx?mid=QwErT&amp;sid=ZxCv&amp;jid=ABCDE" id="aspnetForm">
<div class="page-header"><h1 id="ctl00_masterPageBodyContentPlaceholder_jobTitleGroup">
    Staff Nurse
    <small>(2026-101)</small>
</h1></div>
<div id="ctl00_masterPageBodyContentPlaceholder_jobCategoryFieldRow" class="mb-3 row">
  <label>Category</label><div><p class="form-control-plaintext">Nursing</p></div></div>
<div id="ctl00_masterPageBodyContentPlaceholder_jobLocationFieldRow" class="mb-3 row">
  <label>Location</label><div><p class="form-control-plaintext">Exampletown, County Example, Ireland</p></div></div>
<input type="submit" value="Apply Now" />
<div id="ctl00_masterPageBodyContentPlaceholder_jobDetailsGroup">
  Our nursing home is inviting applications for a <b>Staff Nurse</b>.<br><br>NMBI registration required.
</div>
</form>
<footer>Cookie settings | Head office: Farawaytown, Spain</footer></body></html>"""


def test_candidatemanager_posting_reads_only_the_vacancy_fields():
    url = DETAIL.format(jid="ABCDE")
    job = parse_ats_page(
        JobRawPage(url=url, final_url=url, status_code=200, html=POSTING),
        ats_hints={"title": "", "company": "Example Care", "location": "", "remote_hint": ""},
    )

    assert job.title == "Staff Nurse"
    assert job.company == "Example Care"
    assert job.location == "Exampletown, County Example, Ireland"
    assert "NMBI" in job.body_text
    assert "Farawaytown" not in job.body_text and "Remote Work Policy" not in job.body_text
    assert "remote" not in job.remote_text.lower()


def _http_client(handler) -> HttpClient:
    return HttpClient(
        max_requests=20, max_requests_per_host=20, min_interval_per_host=0.0, transport=httpx.MockTransport(handler),
        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None,
    )


def test_verify_candidatemanager_careers_url_queries_only_the_board():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "www.candidatemanager.net" and request.url.path == "/cm/p/pJobs.aspx":
            return httpx.Response(200, text=BOARD_HTML, headers={"content-type": "text/html"})
        return httpx.Response(404)

    res = verify_proposal(
        Proposal("Example Care", careers_url=DETAIL.format(jid="ABCDE") + "&a=t0KeN%253d"),
        client=_http_client(handler), timeout=5,
    )

    assert res.status == "verified"
    assert res.seed.ats == "candidatemanager" and res.seed.board_identifier == BOARD
    assert seen == [BOARD_URL]


def test_candidatemanager_board_redirecting_off_vendor_is_not_followed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example.test/"})

    with pytest.raises((HttpError, URLRejected)):
        fetch_vendor_jobs("candidatemanager", BOARD, client=_http_client(handler), timeout=5)
    assert seen == ["www.candidatemanager.net"]


def test_run_client_honours_candidatemanager_crawl_delay():
    # robots.txt on www.candidatemanager.net asks for a 10 s crawl delay.
    assert http_factory.VENDOR_MIN_INTERVALS.get("candidatemanager", 0) >= 10.0
    clock = {"now": 0.0, "slept": []}

    def sleep(seconds):
        clock["slept"].append(seconds)
        clock["now"] += seconds

    run = {"settings": {"max_pages": 10, "max_seeds_per_run": 10, "max_requests_per_host": 5, "ats_api_timeout": 5,
                        "max_requests_per_vendor": 50}}
    client = http_factory.client_for_run(
        run, transport=httpx.MockTransport(lambda r: httpx.Response(200, text=BOARD_HTML)),
        resolver=lambda h, p: ["93.184.216.34"], sleep=sleep, clock=lambda: clock["now"], min_interval_per_host=0.0,
    )
    for _ in range(3):
        fetch_vendor_jobs("candidatemanager", BOARD, client=client, timeout=5)
    assert clock["slept"] == [http_factory.VENDOR_MIN_INTERVALS["candidatemanager"]] * 2


def test_candidatemanager_ids_that_differ_only_in_case_are_different_postings():
    # PR #66 review: the mid/sid/jid ids are opaque and case-sensitive, so case variants must not merge.
    upper = canonical_job_identity(DETAIL.format(jid="ABCDE"))
    lower = canonical_job_identity(DETAIL.format(jid="abcde"))
    other_board = canonical_job_identity(DETAIL.format(jid="ABCDE").replace("mid=QwErT", "mid=qwert"))
    assert len({upper.identity_key, lower.identity_key, other_board.identity_key}) == 3


def test_case_distinct_opaque_boards_stay_separate_in_the_seed_inventory():
    # PR #66 review: seed_identity lowercased every non-Workday board, so case-distinct CandidateManager or
    # Occupop boards collapsed into one inventory entry and one employer's vacancies were never queried.
    from ultron import seeds
    from ultron.core.ats.identity import seed_identity
    from ultron.core.models import AtsBoardSeed
    from ultron.profiles import store as profiles

    assert seed_identity("candidatemanager", "QwErT-ZxCv") != seed_identity("candidatemanager", "qwert-ZxCv")
    assert seed_identity("occupop", "AbCdEf123") != seed_identity("occupop", "abcdef123")
    assert seed_identity("greenhouse", "Acme") == seed_identity("greenhouse", "acme")
    boards = ["QwErT-ZxCv", "qwert-ZxCv"]
    rows = [AtsBoardSeed(seed_id=f"candidatemanager:{board}", company=f"Example Care {i}", ats="candidatemanager",
                         board_identifier=board, source_type="agent_generated", verification={"postings": 2})
            for i, board in enumerate(boards)]
    seeds.add_seeds(rows, profile_slug=profiles.EXAMPLE_SLUG)
    stored = {s.board_identifier for s in seeds.load_inventory(profiles.EXAMPLE_SLUG) if s.ats == "candidatemanager"}
    assert stored == set(boards)
