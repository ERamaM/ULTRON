"""Vendor host checks must compare parsed hostnames, not substrings of the URL."""

from __future__ import annotations

from ultron.core.models import CandidateUrl, ExtractedJob
from ultron.stages.fetch import _greenhouse_api_fallback, _smartrecruiters_api_enrich


class RecordingClient:
    def __init__(self):
        self.calls: list[str] = []

    def request_json(self, url, **kwargs):
        self.calls.append(url)
        return {}


def _candidate(url: str, source_key: str = "") -> CandidateUrl:
    return CandidateUrl(query_id="q", query="", source_url=url, normalized_url=url, domain="", source_type="web",
                        source_key=source_key)


def test_smartrecruiters_enrich_ignores_vendor_name_outside_the_host():
    url = "https://example.com/acme/744000012345678?ref=smartrecruiters.com"
    client = RecordingClient()
    job = ExtractedJob(apply_url=url, domain="example.com", title="Careers", body_text="short")
    result, changed = _smartrecruiters_api_enrich(_candidate(url), job, client)
    assert changed is False
    assert result is job
    assert client.calls == []


def test_greenhouse_fallback_uses_eu_api_only_for_eu_greenhouse_hosts():
    client = RecordingClient()
    lookalike = "https://jobs.eu.greenhouse.io.example.com/careers?gh_jid=123"
    _greenhouse_api_fallback(_candidate(lookalike, "greenhouse:acme"), client)
    assert client.calls and client.calls[0].startswith("https://boards-api.greenhouse.io/")

    client = RecordingClient()
    _greenhouse_api_fallback(_candidate("https://job-boards.eu.greenhouse.io/acme/jobs/123"), client)
    assert client.calls and client.calls[0].startswith("https://boards-api.eu.greenhouse.io/")
