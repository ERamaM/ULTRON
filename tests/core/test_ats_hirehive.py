"""HireHive: each tenant board (``https://<tenant>.hirehive.com/``) lists every open job on one public page.

Synthetic fixtures only: the markup mirrors the board's job cards (``hh-job-row``) and its schema.org ``ItemList``
JSON-LD (whose ``type`` attribute HireHive writes with an HTML entity, ``application/ld&#x2B;json``).
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from ultron import http_factory
from ultron.core.ats.board_policy import board_is_valid, vendor_domains
from ultron.core.ats.clients import FETCHERS, AtsClientError, fetch_vendor_jobs
from ultron.core.ats.clients.hirehive import MAX_POSTINGS
from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, infer_vendor_from_host, parse_ats_page
from ultron.core.decision.unit import load_allowed_domains
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import JobRawPage
from ultron.net import HttpClient, HttpError, URLRejected
from ultron.seed_rounds import validate_proposal
from ultron.seeds import PROBE_VENDORS, Proposal, verify_proposal
from ultron.stages.discover import _seed_request_host


class FakeClient:
    def __init__(self, text_value=""):
        self.text_value = text_value
        self.urls: list[str] = []

    def request_json(self, url, method="GET", json_body=None, timeout=None):  # pragma: no cover - never called
        raise AssertionError("HireHive boards are HTML pages")

    def request_text(self, url, timeout=None):
        self.urls.append(url)
        return self.text_value


def _card(href: str, title: str, location: str) -> str:
    return f"""
<a href="{href}" class="bg-white p-4 block mt-3 rounded hh-job-row">
    <h3 class="font-semibold text-xl hh-job-row-title">
        <span class="user-color text-secondary">{title}</span>
    </h3>
    <div class="mt-1 flex flex-col">
        <div class="mt-2 flex items-center text-sm hh-job-row-location">
            <svg viewBox="0 0 24 24"><path d="M0 0"></path></svg>

            {location}
        </div>
        <div class="mt-2 flex items-center text-sm hh-job-row-experience">Full Time</div>
    </div>
</a>"""


def _board(cards: str, item_list: str = "") -> str:
    ld = f'<script type="application/ld&#x2B;json">{item_list}</script>' if item_list else ""
    return f"""<!DOCTYPE html><html><head><title>Example Care Ltd. jobs</title>{ld}</head>
<body class="jobs-page"><div id="jobs" class="bg-gray-200 hh-job-listings"><div class="hh-jobs-openings">
{cards}
</div></div></body></html>"""


BOARD = _board(
    _card("/staff-nurse-exampletown-co-example-Ab12Cd", "Staff Nurse", "Exampletown,             Ireland ")
    + _card("/clinical-nurse-manager-sampleville-Ef34Gh", "Clinical Nurse Manager", "Sampleville, Ireland")
    + _card("https://other-tenant.hirehive.com/foreign-job-Zz99Zz", "Foreign Job", "Elsewhere")
    + _card("https://evil.example.test/phish", "Off Vendor", "Nowhere"),
    item_list='{"@context":"https://schema.org","@type":"ItemList","itemListElement":['
    '{"@type":"ListItem","name":"Staff Nurse",'
    '"url":"https://examplecare.hirehive.com/staff-nurse-exampletown-co-example-Ab12Cd?source=google_jobs_apply"}]}',
)


def fetch(board: str, client: FakeClient):
    return fetch_vendor_jobs("hirehive", board, client=client, timeout=5)


def test_hirehive_is_a_registered_vendor():
    assert "hirehive" in FETCHERS
    assert vendor_domains("hirehive") == ("hirehive.com",)
    assert infer_vendor_from_host("examplecare.hirehive.com") == "hirehive"
    assert http_factory.vendor_for_host("examplecare.hirehive.com") == "hirehive"
    assert {"hirehive.com"} <= set(load_allowed_domains())
    assert validate_proposal({"company": "Example Care", "ats": "hirehive", "board": "examplecare"}) == []


def test_hirehive_board_makes_one_request_and_parses_cards_with_locations():
    client = FakeClient(BOARD)

    postings = fetch("examplecare", client)

    assert client.urls == ["https://examplecare.hirehive.com/"]
    assert [(p.title, p.apply_url, p.location) for p in postings] == [
        ("Staff Nurse", "https://examplecare.hirehive.com/staff-nurse-exampletown-co-example-Ab12Cd",
         "Exampletown, Ireland"),
        ("Clinical Nurse Manager", "https://examplecare.hirehive.com/clinical-nurse-manager-sampleville-Ef34Gh",
         "Sampleville, Ireland"),
    ]


def test_hirehive_falls_back_to_item_list_and_strips_tracking_query():
    item_list = (
        '{"@context":"https://schema.org","@type":"ItemList","itemListElement":['
        '{"@type":"ListItem","name":"Staff Nurse",'
        '"url":"https://examplecare.hirehive.com/staff-nurse-Ab12Cd?source=google_jobs_apply","position":1},'
        '{"@type":"ListItem","name":"Off Vendor","url":"https://evil.example.test/x","position":2},'
        '{"@type":"ListItem","name":"","url":"https://examplecare.hirehive.com/untitled-Qq11Qq","position":3}]}'
    )

    postings = fetch("examplecare", FakeClient(_board("", item_list)))

    assert [(p.title, p.apply_url, p.location) for p in postings] == [
        ("Staff Nurse", "https://examplecare.hirehive.com/staff-nurse-Ab12Cd", ""),
    ]


def test_hirehive_board_with_no_openings_is_empty_not_an_error():
    assert fetch("examplecare", FakeClient(_board(""))) == []


def test_hirehive_page_without_board_markup_is_an_error():
    # A page that is not a job board (the vendor's marketing site, an error page) must not verify as an empty board.
    with pytest.raises(AtsClientError, match="unparseable_board"):
        fetch("examplecare", FakeClient("<html><body><h1>Hire better</h1></body></html>"))


def test_hirehive_bounds_the_number_of_postings():
    cards = "".join(_card(f"/job-{i}-Ab{i}", f"Nurse {i}", "Ireland") for i in range(MAX_POSTINGS + 25))

    postings = fetch("examplecare", FakeClient(_board(cards)))

    assert len(postings) == MAX_POSTINGS


@pytest.mark.parametrize(
    "board", ["examplecare", "example-care", "examplecare.hirehive.com", "a/b", "..", "https://x.test", ""],
)
def test_hirehive_board_identifier_is_one_tenant_label(board):
    client = FakeClient(BOARD)
    if board in {"examplecare", "example-care"}:
        assert board_is_valid("hirehive", board)
        assert fetch(board, client)
        assert client.urls == [f"https://{board}.hirehive.com/"]
    else:
        with pytest.raises(AtsClientError, match="invalid_board"):
            fetch(board, client)
        assert client.urls == []


def test_hirehive_rejects_invalid_board_before_any_request():
    client = FakeClient(BOARD)
    with pytest.raises(AtsClientError, match="invalid_board"):
        fetch("evil.test/x", client)
    assert client.urls == []


@pytest.mark.parametrize(
    ("url", "board"),
    [
        ("https://examplecare.hirehive.com/", "examplecare"),
        ("https://examplecare.hirehive.com/staff-nurse-exampletown-Ab12Cd?source=google_jobs_apply", "examplecare"),
        ("https://ExampleCare.hirehive.com/jobs", "examplecare"),
        ("https://www.hirehive.com/", None),
        ("https://www.hirehive.com/terms-of-service/", None),
        ("https://app.hirehive.com/login", None),
        ("https://hirehive.com/", None),
    ],
)
def test_hirehive_seed_from_careers_url(url, board):
    seed = extract_seed_from_url(url, "Example Care")
    if board is None:
        assert seed is None
    else:
        assert seed is not None and seed.ats == "hirehive" and seed.board_identifier == board
        assert seed_identity(seed.ats, seed.board_identifier) == ("hirehive", board)


def test_hirehive_is_not_guessed_by_slug():
    # Slug probing is the most expensive part of seed verification; HireHive boards come from a careers URL or an
    # explicit ats + board pair only.
    assert "hirehive" not in PROBE_VENDORS


def test_hirehive_seeds_use_their_tenant_host_for_the_per_host_cap():
    one = SimpleNamespace(ats="hirehive", board_identifier="ExampleCare")
    two = SimpleNamespace(ats="hirehive", board_identifier="othercare")
    assert _seed_request_host(one) == "examplecare.hirehive.com"
    assert _seed_request_host(two) == "othercare.hirehive.com"


def test_hirehive_posting_identity_drops_the_tracking_query():
    plain = canonical_job_identity("https://examplecare.hirehive.com/staff-nurse-Ab12Cd")
    tracked = canonical_job_identity("https://examplecare.hirehive.com/staff-nurse-Ab12Cd?source=google_jobs_apply")
    assert plain.identity_key == tracked.identity_key


POSTING = """<!DOCTYPE html><html><head><title>Staff Nurse | Example Care Ltd. | Exampletown</title>
<meta property="og:site_name" content="HireHive.com" />
<script type="application/ld&#x2B;json">
{"@context":"https://schema.org","@type":"JobPosting","title":"Staff Nurse",
 "description":"<p>Registered nurse (NMBI) for our residential service.</p>",
 "url":"https://examplecare.hirehive.com/staff-nurse-Ab12Cd?source=google_jobs_apply","datePosted":"2026-11-06",
 "employmentType":"FULL_TIME","hiringOrganization":{"@type":"Organization","name":"Example Care Ltd."},
 "jobLocation":{"@type":"Place","address":{"@type":"PostalAddress","addressCountry":"IE",
 "addressLocality":"Exampletown, Co. Example"}},"validThrough":"2099-12-18"}
</script></head><body>
<h1 class="text-2xl hh-job-title">Staff Nurse</h1>
<div class="hh-job-information"><div class="hh-job-location">Exampletown, Co. Example, Ireland</div>
<div class="hh-job-type">Full Time</div></div>
<div class="ql-editor hh-job-description"><p>Registered nurse (NMBI) for our residential service.</p></div>
<form class="hh-application-form"><label>Country<select name="country">
<option value="1">Ireland</option><option value="245">Zambia </option><option value="246">Zimbabwe </option>
<option value="495">Work Remotely</option></select></label></form></body></html>"""


def test_hirehive_posting_page_is_parsed():
    url = "https://examplecare.hirehive.com/staff-nurse-Ab12Cd"
    job = parse_ats_page(JobRawPage(url=url, final_url=url, status_code=200, html=POSTING))

    assert job.title == "Staff Nurse"
    assert job.company == "Example Care Ltd."
    assert "NMBI" in job.body_text


def test_hirehive_posting_ignores_the_application_form_and_keeps_the_country():
    # Session 5, offline replay of a real posting page: the application form's country list ends with a
    # "Work Remotely" option, which made an on-site nursing job read as remote, and the JSON-LD location
    # ("<town>, Co. <county> | IE") won over the page's own location line, which names the country.
    url = "https://examplecare.hirehive.com/staff-nurse-Ab12Cd"
    job = parse_ats_page(JobRawPage(url=url, final_url=url, status_code=200, html=POSTING))

    assert "remote" not in job.remote_text.lower()
    assert job.location == "Exampletown, Co. Example, Ireland"
    assert "Zimbabwe" not in job.body_text


def _http_client(handler) -> HttpClient:
    return HttpClient(
        max_requests=20, max_requests_per_host=20, min_interval_per_host=0.0, transport=httpx.MockTransport(handler),
        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None,
    )


def test_run_client_paces_hirehive_tenants_slowly():
    # Session 5 (S5): ten HireHive boards verified 0.5 s apart, then the eleventh got HTTP 429 twice and the vendor
    # breaker blocked HireHive. Every tenant shares one rate limit, so the run client spaces them out.
    assert http_factory.VENDOR_MIN_INTERVALS.get("hirehive", 0) >= 3.0
    clock = {"now": 0.0, "slept": []}

    def sleep(seconds):
        clock["slept"].append(seconds)
        clock["now"] += seconds

    run = {"settings": {"max_pages": 10, "max_seeds_per_run": 10, "max_requests_per_host": 5, "ats_api_timeout": 5,
                        "max_requests_per_vendor": 50}}
    client = http_factory.client_for_run(
        run, transport=httpx.MockTransport(lambda r: httpx.Response(200, text=BOARD)),
        resolver=lambda h, p: ["93.184.216.34"], sleep=sleep, clock=lambda: clock["now"], min_interval_per_host=0.0,
    )
    for board in ("examplecare", "othercare", "thirdcare"):
        fetch_vendor_jobs("hirehive", board, client=client, timeout=5)
    assert clock["slept"] == [http_factory.VENDOR_MIN_INTERVALS["hirehive"]] * 2


def test_verify_hirehive_careers_url_queries_only_the_tenant_board():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "examplecare.hirehive.com" and request.url.path == "/":
            return httpx.Response(200, text=BOARD, headers={"content-type": "text/html"})
        return httpx.Response(404)

    res = verify_proposal(
        Proposal("Example Care", careers_url="https://examplecare.hirehive.com/staff-nurse-Ab12Cd"),
        client=_http_client(handler), timeout=5,
    )

    assert res.status == "verified"
    assert res.seed.ats == "hirehive" and res.seed.board_identifier == "examplecare"
    assert seen == ["https://examplecare.hirehive.com/"]


def test_hirehive_board_redirecting_off_vendor_is_not_followed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://evil.example.test/"})

    with pytest.raises((HttpError, URLRejected)):
        fetch_vendor_jobs("hirehive", "examplecare", client=_http_client(handler), timeout=5)
    assert seen == ["examplecare.hirehive.com"]
