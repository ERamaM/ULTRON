"""More ATS client edge cases ported from the MARK II project (EU retries, second feeds, malformed payloads)."""

from __future__ import annotations

import pytest

from ultron.core.ats.clients import ashby, base, comeet, factorial, icims, lever, personio
from ultron.core.ats.clients.base import AtsClientError


class FC:
    def __init__(self, json_fn=None, text_fn=None):
        self.json_fn, self.text_fn = json_fn, text_fn

    def request_json(self, url, method="GET", json_body=None, timeout=None):
        return self.json_fn(url)

    def request_text(self, url, timeout=None):
        return self.text_fn(url)


def test_lever_empty_primary_then_eu():
    n = {"c": 0}

    def j(u):
        n["c"] += 1
        return [] if n["c"] == 1 else [{"text": "EU Lever", "hostedUrl": "https://lever/1"}]
    assert lever.fetch_lever_jobs("acme", client=FC(j))[0].title == "EU Lever"


def test_personio_second_feed_after_non_xml():
    n = {"c": 0}

    def t(u):
        n["c"] += 1
        return "not xml" if n["c"] == 1 else "<workzag-jobs><position><name>Second feed</name><url>https://p/2</url></position></workzag-jobs>"
    assert personio.fetch_personio_jobs("acme", client=FC(text_fn=t))[0].title == "Second feed"


def test_personio_empty_and_invalid():
    assert personio.fetch_personio_jobs("acme", client=FC(text_fn=lambda u: "")) == []
    with pytest.raises(AtsClientError, match="invalid_payload"):
        personio.fetch_personio_jobs("acme", client=FC(text_fn=lambda u: "not xml"))


def test_personio_recovers_from_truncated_xml():
    r = iter(["<workzag-jobs><position>", "<workzag-jobs><position><id>2</id><name>Recovered</name></position></workzag-jobs>"])
    assert personio.fetch_personio_jobs("acme", client=FC(text_fn=lambda u: next(r)))[0].title == "Recovered"


def test_parse_card_listings_boundaries():
    assert base.parse_card_listings(
        '<span class="job-title"></span><span class="job-title">No href</span>'
        '<div class="job-title"><a href="/1">Role</a></div><div class="job-title"><a href="/1">Role</a></div>',
        "job-title") == [{"title": "Role", "href": "/1", "location": ""}]
    assert base.locator_url("") == ""
    assert base.locator_url("acme.example", "", "/jobs/") == "https://acme.example/jobs"


def test_ashby_malformed_payloads():
    for payload in ([], {}, {"jobs": "bad"}):
        with pytest.raises(AtsClientError):
            ashby.fetch_ashby_jobs("acme", client=FC(lambda u, p=payload: p))


def test_comeet_parse_positions_rejects_foreign_and_short_links():
    assert comeet._parse_positions(
        '<a href="https://other.example/jobs/acme/company/position/role">Other</a>'
        '<a href="/jobs/acme/company">Too short</a><a href="/jobs/acme/company/position/role"></a>', "acme") == []


def test_factorial_list_payload_and_bad_rows():
    assert factorial.fetch_factorial_jobs("acme", client=FC(lambda u: [{"name": "List role", "url": "https://f/1"}], lambda u: ""))[0].title == "List role"
    assert factorial.fetch_factorial_jobs("acme", client=FC(lambda u: ["not-a-job"], lambda u: "")) == []


def test_icims_dedupes_and_rejects_hostless_locator():
    with pytest.raises(AtsClientError):
        icims.fetch_icims_jobs("/missing-host", client=FC(text_fn=lambda u: ""))
    dup = '<a href="/job/1"><span class="iCIMS_JobsTable_title">One</span></a>' * 2
    assert len(icims.fetch_icims_jobs("acme.example", client=FC(text_fn=lambda u: dup))) == 1


def test_comeet_parse_positions_keeps_absolute_links_on_the_hosted_com_domain():
    # Hosted Comeet positions live on www.comeet.com; "comeet.co" is a substring of that host, so the
    # path must be taken from the parsed URL, not by splitting the string on "comeet.co".
    html = (
        '<a href="https://www.comeet.com/jobs/acme/41.001/data-scientist/C7.E11">Data Scientist</a>'
        '<a href="https://www.comeet.co/jobs/acme/41.001/ml-engineer/C7.E12">ML Engineer</a>'
        '<a href="https://comeet.com.evil.example/jobs/acme/41.001/fake/C7.E13">Fake</a>'
    )
    postings = comeet._parse_positions(html, "acme")
    assert [(p.title, p.apply_url) for p in postings] == [
        ("Data Scientist", "https://www.comeet.com/jobs/acme/41.001/data-scientist/C7.E11"),
        ("ML Engineer", "https://www.comeet.co/jobs/acme/41.001/ml-engineer/C7.E12"),
    ]

