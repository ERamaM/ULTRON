"""Challenge pages and JS-only shells are recorded as fetch errors, not emitted as postings."""

from __future__ import annotations

import httpx
import pytest

from ultron import http_factory, profiles, runs, seeds
from ultron.core.models import CandidateUrl
from ultron.stages.fetch import fetch

CAPTCHA_PAGE = """<html><head><title>Attention required</title></head><body>
<h1>Please verify you are human</h1><p>Complete the CAPTCHA below to continue.</p>
<div id="challenge"></div></body></html>"""

JUST_A_MOMENT_PAGE = """<html><head><title>Just a moment...</title>
<script src="/cdn-cgi/challenge-platform/orchestrate.js"></script></head>
<body><noscript>Enable JavaScript and cookies to continue</noscript></body></html>"""

JS_SHELL_PAGE = """<html><head><title>Careers</title>
<script src="/static/js/main.4f2a.js"></script></head>
<body><div id="root"></div></body></html>"""

SHORT_REAL_POSTING = """<html><head><title>Data Engineer - Example Labs</title>
<script src="/analytics.js"></script></head><body>
<h1>Data Engineer</h1>
<p>Example Labs is hiring a Data Engineer, fully remote within Spain.</p>
<p>You will build batch pipelines with Python, SQL and Airflow.</p>
<p>Apply with your CV.</p></body></html>"""

LONG_POSTING_MENTIONING_SECURITY_CHECK = (
    "<html><head><title>Security Engineer</title></head><body><h1>Security Engineer</h1>"
    + "<p>You will run the quarterly security check of our cloud estate and harden CI pipelines.</p>" * 30
    + "</body></html>"
)


SECURITY_CHECK_CHALLENGE_PAGE = """<html><head><title>Security check</title></head><body>
<p>Security check: please enable JavaScript and cookies in your browser.</p></body></html>"""

SPLIT_CHALLENGE_PAGE = """<html><head><title>One more step</title></head><body>
<p>Verify you are <b>human</b></p></body></html>"""

# A JavaScript bot challenge served by some careers sites instead of the page (seen live in a backend run).
CLIENT_CHALLENGE_PAGE = """<html><head><title>Client Challenge</title><script src="/challenge.js"></script></head>
<body><p>JavaScript is disabled in your browser.</p><p>Please enable JavaScript to proceed.</p>
<p>A required part of this site couldn't load. This may be due to a browser extension, network issues, or
browser settings. Please check your connection, disable any ad blockers, or try using a different browser.</p>
</body></html>"""


def _short_posting(sentence: str) -> str:
    return (
        "<html><head><title>Backend Engineer - Example Labs</title></head><body><h1>Backend Engineer</h1>"
        f"<p>Example Labs is hiring a Backend Engineer, fully remote within Spain. {sentence}</p></body></html>"
    )


@pytest.fixture(autouse=True)
def _no_packs(tmp_path, monkeypatch):
    empty = tmp_path / "no_packs"
    empty.mkdir()
    monkeypatch.setattr(seeds, "packs_dir", lambda: empty)


def _fetch_single(html: str) -> tuple[list[dict], list[dict]]:
    profiles.install_example()
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)),
        resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None, min_interval_per_host=0.0,
    )
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    d = runs.run_dir(run_id)
    url = "https://careers.example.org/jobs/42"
    runs.write_jsonl(d / "candidates.jsonl", [
        CandidateUrl(query_id="web:1", query="", source_url=url, normalized_url=url, domain="careers.example.org",
                     source_type="web_search", source_key="web_search"),
    ])
    runs.mark_stage(run_id, "discover_ats")
    fetch(run_id)
    return runs.read_jsonl(d / "postings.jsonl"), runs.read_jsonl(d / "fetch_errors.jsonl")


@pytest.mark.parametrize(
    "html", [CAPTCHA_PAGE, JUST_A_MOMENT_PAGE, SPLIT_CHALLENGE_PAGE, SECURITY_CHECK_CHALLENGE_PAGE, CLIENT_CHALLENGE_PAGE]
)
def test_challenge_page_recorded_as_blocked(html):
    postings, errors = _fetch_single(html)
    assert postings == []
    assert errors == [{"url": "https://careers.example.org/jobs/42", "error": "blocked", "status": 200}]


def test_js_only_shell_recorded():
    postings, errors = _fetch_single(JS_SHELL_PAGE)
    assert postings == []
    assert errors == [{"url": "https://careers.example.org/jobs/42", "error": "js_shell", "status": 200}]


@pytest.mark.parametrize("html", [SHORT_REAL_POSTING, LONG_POSTING_MENTIONING_SECURITY_CHECK])
def test_real_postings_still_pass(html):
    postings, errors = _fetch_single(html)
    assert errors == [] and len(postings) == 1


@pytest.mark.parametrize("sentence", [
    "Our signup flow is captchaless.",
    "You will maintain our security checker service.",
    "Our forms are protected by reCAPTCHA.",
    "Applicants must pass a security check.",
    "You will run regular security checks.",
    "You will solve each client challenge with our browser extension team.",
])
def test_challenge_words_inside_other_words_do_not_block(sentence):
    postings, errors = _fetch_single(_short_posting(sentence))
    assert errors == [] and len(postings) == 1


def test_smartrecruiters_metadata_without_description_does_not_skip_classification(monkeypatch):
    from ultron.stages import fetch as fetch_stage

    def metadata_only(candidate, job, client):
        return job.model_copy(update={"title": "Senior Engineer", "company": "Example"}), True, False

    monkeypatch.setattr(fetch_stage, "_smartrecruiters_api_lookup", metadata_only)
    postings, errors = _fetch_single(CAPTCHA_PAGE)
    assert postings == []
    assert errors == [{"url": "https://careers.example.org/jobs/42", "error": "blocked", "status": 200}]


def test_smartrecruiters_description_skips_classification_even_if_unchanged(monkeypatch):
    from ultron.stages import fetch as fetch_stage

    def described(candidate, job, client):
        return job, True, True  # the API returned the same description extraction already found

    monkeypatch.setattr(fetch_stage, "_smartrecruiters_api_lookup", described)
    postings, errors = _fetch_single(CAPTCHA_PAGE)
    assert errors == [] and len(postings) == 1
