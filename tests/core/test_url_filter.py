from __future__ import annotations

from ultron.core.discovery import url_filter
from ultron.core.discovery.url_filter import filter_candidates
from ultron.core.models import SearchHit


def _hit(url: str) -> SearchHit:
    return SearchHit(
        query_id="q1",
        query="ml engineer",
        rank=1,
        title="ML Engineer",
        snippet="",
        url=url,
    )


def test_filter_candidates_blocks_subdomains():
    candidates, discards = filter_candidates(
        [_hit("https://es.indeed.com/viewjob?jk=123")],
        {"allowed_ats_domains": ["greenhouse.io"], "blocked_final_domains": ["indeed.com"]},
    )

    assert candidates == []
    assert discards == [
        {
            "url": "https://es.indeed.com/viewjob?jk=123",
            "reason": "blocked_domain",
            "detail": "es.indeed.com",
        }
    ]


def test_filter_candidates_keeps_allowed_subdomains():
    candidates, discards = filter_candidates(
        [_hit("https://boards.greenhouse.io/acme/jobs/123")],
        {"allowed_ats_domains": ["greenhouse.io"], "blocked_final_domains": ["indeed.com"]},
    )

    assert discards == []
    assert len(candidates) == 1
    assert candidates[0].domain == "boards.greenhouse.io"


def test_filter_candidates_adds_missing_scheme():
    candidates, discards = filter_candidates(
        [_hit("boards.greenhouse.io/acme/jobs/123")],
        {"allowed_ats_domains": ["greenhouse.io"], "blocked_final_domains": []},
    )

    assert discards == []
    assert candidates[0].normalized_url == "https://boards.greenhouse.io/acme/jobs/123"


def test_filter_candidates_discards_non_http_urls():
    candidates, discards = filter_candidates(
        [_hit("javascript:alert(1)")],
        {"allowed_ats_domains": ["greenhouse.io"], "blocked_final_domains": []},
    )

    assert candidates == []
    assert discards[0]["reason"] == "invalid_url"


def test_url_normalization_preserves_job_ids_and_removes_tracking():
    normalized = url_filter.normalize_url("BOARDS.GREENHOUSE.IO/acme/jobs/1/?gh_jid=42&utm_source=x&foo=bar")
    assert normalized == "https://boards.greenhouse.io/acme/jobs/1?gh_jid=42"
    assert url_filter.normalize_url("//example.com/jobs/") == "https://example.com/jobs"
    assert url_filter.domain_of(normalized) == "boards.greenhouse.io"
    assert url_filter._extract_ats_board("Acme [greenhouse:acme]") == ("greenhouse", "acme")
    assert url_filter._extract_ats_board("plain query") is None
    assert url_filter._is_domain_allowed("jobs.example.com", ["example.com"])
    assert url_filter._is_domain_blocked("jobs.example.com", {"example.com"})


def test_custom_greenhouse_urls_and_rebalance_contract():
    custom = url_filter.canonicalize_url_for_allowlist(
        "https://custom.example/job?gh_jid=123", "Acme [greenhouse:acme]", ["greenhouse.io"]
    )
    assert "boards.greenhouse.io/acme/jobs/123" in custom
    unchanged = url_filter.canonicalize_url_for_allowlist("https://other.example/job", "plain", ["greenhouse.io"])
    assert unchanged == "https://other.example/job"
    assert url_filter.rebalance_candidates([], 2) == ([], [])
    one = [_hit("https://boards.greenhouse.io/acme/1")]
    assert url_filter.rebalance_candidates(one, 2) == (one, [])
    kept, overflow = url_filter.rebalance_candidates(one * 3, 1)
    assert len(kept) == 1 and len(overflow) == 2
    assert url_filter.rebalance_candidates(one, 0) == ([], one)


def test_filter_candidates_rejects_linkedin_duplicates_and_keeps_vendor_metadata():
    hits = [_hit("https://www.linkedin.com/jobs/view/1"), _hit("https://boards.greenhouse.io/acme/1")]
    hits.append(_hit("https://boards.greenhouse.io/acme/1"))
    candidates, discards = filter_candidates(hits, {"allowed_ats_domains": [], "blocked_final_domains": []})
    assert len(candidates) == 1
    assert candidates[0].matched_vendor == "greenhouse"
    assert {item["reason"] for item in discards} == {"linkedin_forbidden", "duplicate_url"}


def test_filter_candidates_rejects_non_admitted_target_before_fetch():
    candidates, discards = filter_candidates(
        [_hit("http://127.0.0.1:80/private")],
        {"allowed_ats_domains": [], "blocked_final_domains": []},
    )

    assert candidates == []
    assert discards == [
        {
            "url": "http://127.0.0.1",
            "reason": "url_not_admitted",
            "detail": "non_public_address",
        }
    ]


def test_filter_candidates_rejects_vendor_marketing_and_documentation_pages():
    hits = [
        _hit("https://developers.pinpointhq.com/docs/channels"),
        _hit("https://status.comeet.com"),
        _hit("https://www.comeet.com"),
        _hit("https://www.comeet.com/jobs/acme/41.001"),
    ]

    candidates, discards = filter_candidates(
        hits,
        {"allowed_ats_domains": ["pinpointhq.com", "comeet.com"], "blocked_final_domains": []},
    )

    assert [candidate.normalized_url for candidate in candidates] == ["https://www.comeet.com/jobs/acme/41.001"]
    assert [item["reason"] for item in discards] == ["ats_non_board_url"] * 3
