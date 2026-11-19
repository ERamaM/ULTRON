"""API enrichers must survive malformed vendor JSON and leave the job unchanged."""

from __future__ import annotations

import pytest

from ultron.core.models import CandidateUrl, ExtractedJob
from ultron.stages.fetch import _bamboohr_api_enrich, _smartrecruiters_api_enrich


class FakeClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[str] = []

    def request_json(self, url, **kwargs):
        self.calls.append(url)
        return self.payload


def _sr_case():
    url = "https://jobs.smartrecruiters.com/acme/744000012345678-ml-engineer"
    candidate = CandidateUrl(query_id="smartrecruiters:acme", query="", source_url=url, normalized_url=url,
                             domain="jobs.smartrecruiters.com", source_type="ats_api",
                             source_key="smartrecruiters:acme")
    job = ExtractedJob(apply_url=url, domain="jobs.smartrecruiters.com", title="ML Engineer", company="Acme",
                       body_text="short")
    return candidate, job


def _bamboo_case():
    url = "https://acme.bamboohr.com/careers/42"
    candidate = CandidateUrl(query_id="bamboohr:acme", query="", source_url=url, normalized_url=url,
                             domain="acme.bamboohr.com", source_type="ats_api", source_key="bamboohr:acme")
    job = ExtractedJob(apply_url=url, domain="acme.bamboohr.com", title="ML Engineer", body_text="short")
    return candidate, job


@pytest.mark.parametrize("payload", [
    {"name": "ML Engineer", "company": "Acme"},
    {"name": "ML Engineer", "jobAd": ["unexpected"]},
    {"name": "ML Engineer", "jobAd": {"sections": ["unexpected"]}},
])
def test_smartrecruiters_malformed_payload_leaves_job_unchanged(payload):
    candidate, job = _sr_case()
    client = FakeClient(payload)
    assert _smartrecruiters_api_enrich(candidate, job, client) == (job, False)
    assert client.calls


def test_smartrecruiters_well_formed_payload_still_enriches():
    candidate, job = _sr_case()
    payload = {
        "name": "Senior ML Engineer",
        "company": {"name": "Acme Corp"},
        "jobAd": {"sections": {"jobDescription": {"text": "<p>Train and ship models.</p>"}}},
    }
    enriched, used = _smartrecruiters_api_enrich(candidate, job, FakeClient(payload))
    assert used and enriched.company == "Acme Corp" and "Train and ship models." in enriched.body_text


def test_smartrecruiters_missing_optional_objects_still_enriches():
    candidate, job = _sr_case()
    enriched, used = _smartrecruiters_api_enrich(candidate, job, FakeClient({"name": "Senior ML Engineer"}))
    assert used and enriched.title == "Senior ML Engineer" and enriched.company == "Acme"


@pytest.mark.parametrize("payload", [{"result": ["unexpected"]}, {"result": "unexpected"}])
def test_bamboohr_malformed_payload_leaves_job_unchanged(payload):
    candidate, job = _bamboo_case()
    client = FakeClient(payload)
    assert _bamboohr_api_enrich(candidate, job, client) == (job, False)
    assert client.calls


def test_bamboohr_well_formed_payload_still_enriches():
    candidate, job = _bamboo_case()
    payload = {"result": {"jobOpening": {"jobOpeningName": "Senior ML Engineer",
                                         "description": "<p>" + "Build ranking models. " * 5 + "</p>"}}}
    enriched, used = _bamboohr_api_enrich(candidate, job, FakeClient(payload))
    assert used and enriched.title == "Senior ML Engineer"
