"""BambooHR's workplace type (``locationType`` / ``isRemote``) must reach the remote gate."""

from __future__ import annotations

import pytest

from ultron.core.ats.clients.bamboohr import bamboohr_workplace, fetch_bamboohr_jobs
from ultron.core.models import CandidateUrl, ExtractedJob
from ultron.core.rules.hard_filters import apply_hard_filters
from ultron.stages.fetch import _bamboohr_api_enrich

# Company blurb modelled on a live onsite posting whose prose mentions remote staff elsewhere.
_BLURB = (
    "<p>About us: Examplecorp builds logistics software. Our main office is in Valencia, we have a second "
    "office in Seville and some remote workers abroad. The team ships data products every week.</p>"
)


class FakeClient:
    def __init__(self, payload):
        self.payload = payload

    def request_json(self, url, **kwargs):
        return self.payload

    def request_text(self, url, **kwargs):  # pragma: no cover - the JSON list is enough here
        return ""


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"locationType": "0"}, "On-site"),
        ({"locationType": 0}, "On-site"),
        ({"locationType": "1"}, "Remote"),
        ({"locationType": "2"}, "Hybrid"),
        ({"isRemote": True}, "Remote"),
        ({"isRemote": None, "locationType": None}, ""),
        ({"locationType": "9"}, ""),
        ({}, ""),
    ],
)
def test_bamboohr_workplace_mapping(row, expected):
    assert bamboohr_workplace(row) == expected


def test_bamboohr_list_carries_the_workplace_type():
    payload = {"result": [
        {"id": "501", "jobOpeningName": "Senior Backend Developer", "location": {"city": "Valencia"},
         "isRemote": None, "locationType": "0"},
        {"id": "502", "jobOpeningName": "Data Engineer", "location": {"city": "Seville"}, "locationType": "1"},
    ]}
    postings = fetch_bamboohr_jobs("examplecorp", client=FakeClient(payload))
    assert [(p.title, p.remote_hint) for p in postings] == [
        ("Senior Backend Developer", "On-site"),
        ("Data Engineer", "Remote"),
    ]


def test_bamboohr_onsite_detail_fails_the_full_remote_gate():
    url = "https://examplecorp.bamboohr.com/careers/501"
    candidate = CandidateUrl(query_id="bamboohr:examplecorp", query="", source_url=url, normalized_url=url,
                             domain="examplecorp.bamboohr.com", source_type="ats_api",
                             source_key="bamboohr:examplecorp")
    job = ExtractedJob(apply_url=url, domain="examplecorp.bamboohr.com", title="Senior Backend Developer",
                       body_text="short")
    payload = {"result": {"jobOpening": {
        "jobOpeningName": "OFFICE VALENCIA - Senior Backend Developer",
        "location": {"city": "Valencia", "state": "Valencia", "addressCountry": "Spain"},
        "locationType": "0",
        "description": _BLURB,
    }}}
    enriched, used = _bamboohr_api_enrich(candidate, job, FakeClient(payload))
    assert used
    assert "On-site" in enriched.remote_text
    accepted, reason = apply_hard_filters(enriched, ["bamboohr.com"], remote_required=True, target_regions=["Spain"])
    assert (accepted, reason) == (False, "hybrid_or_not_explicit_remote")
