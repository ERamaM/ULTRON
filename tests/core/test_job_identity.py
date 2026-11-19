from __future__ import annotations

import json
from pathlib import Path

import pytest

from ultron.core import job_identity
from ultron.core.job_identity import canonical_job_identity

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "job_identity_cases.json"


def test_vendor_variants_share_stable_identity_without_tracking_params():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for group in cases["equivalent_groups"]:
        identities = [
            canonical_job_identity(
                url,
                source_type=group["source_type"],
                source_key=group["source_key"],
            )
            for url in group["urls"]
        ]
        assert {identity.identity_key for identity in identities} == {group["expected_identity_key"]}, group["name"]
        assert all(identity.fallback_reason is None for identity in identities)
        assert all("utm_" not in identity.canonical_url for identity in identities)
        assert all("#" not in identity.canonical_url for identity in identities)


def test_generic_url_normalization_is_idna_safe_and_conservative():
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    for case in cases["generic_cases"]:
        identity = canonical_job_identity(case["url"])
        assert identity.canonical_url == case["expected_canonical_url"]
        assert identity.identity_key == case["expected_identity_key"]


def test_distinct_job_ids_never_collide():
    first = canonical_job_identity(
        "https://boards.greenhouse.io/acme/jobs/100?utm_source=one",
        source_type="ats_api",
        source_key="greenhouse:acme",
    )
    second = canonical_job_identity(
        "https://boards.greenhouse.io/acme/jobs/101?utm_source=one",
        source_type="ats_api",
        source_key="greenhouse:acme",
    )
    assert first.identity_key != second.identity_key


def test_vendor_path_id_wins_over_tracking_query_id():
    identity = canonical_job_identity(
        "https://boards.greenhouse.io/acme/jobs/123?id=tracking-value",
        source_type="greenhouse",
        source_key="greenhouse:acme",
    )
    assert identity.identity_key == "greenhouse:acme:123"


def test_unparseable_url_uses_auditable_conservative_fallback(caplog):
    identity = canonical_job_identity("javascript:alert(1)")
    assert identity.identity_key == "raw:javascript:alert(1)"
    assert identity.canonical_url == "javascript:alert(1)"
    assert identity.fallback_reason == "unsupported_scheme"
    assert "job_identity_fallback" in caplog.text


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("", "empty_url"),
        ("https:///jobs", "missing_host"),
        ("https://example.com:not-a-port/jobs", "invalid_port"),
    ],
)
def test_malformed_urls_use_distinct_auditable_fallback_reasons(url, reason):
    identity = canonical_job_identity(url)

    assert identity.identity_key == f"raw:{url.casefold()}"
    assert identity.canonical_url == url
    assert identity.fallback_reason == reason


def test_specific_query_job_id_beats_generic_id():
    specific = canonical_job_identity(
        "https://careers.acme.test/opening?id=1&jobid=55555",
        source_type="greenhouse",
        source_key="greenhouse:acme",
    )
    other = canonical_job_identity(
        "https://careers.acme.test/opening?id=1&jobid=99999",
        source_type="greenhouse",
        source_key="greenhouse:acme",
    )
    only_id = canonical_job_identity(
        "https://careers.acme.test/opening?id=1",
        source_type="greenhouse",
        source_key="greenhouse:acme",
    )

    assert specific.identity_key == "greenhouse:acme:55555"
    assert other.identity_key == "greenhouse:acme:99999"
    assert only_id.identity_key == "greenhouse:acme:1"


def test_workday_identity_uses_requisition_suffix_not_locale():
    analyst = canonical_job_identity(
        "https://acme.wd3.myworkdayjobs.com/es-419/careers/job/Madrid/Analista",
        source_type="workday",
        source_key="workday:acme.wd3.myworkdayjobs.com/careers",
    )
    scientist = canonical_job_identity(
        "https://acme.wd3.myworkdayjobs.com/es-419/careers/job/Madrid/Cientifico",
        source_type="workday",
        source_key="workday:acme.wd3.myworkdayjobs.com/careers",
    )
    requisition = canonical_job_identity(
        "https://acme.wd3.myworkdayjobs.com/en-US/careers/job/Madrid/Analista_R-2048",
        source_type="workday",
        source_key="workday:acme.wd3.myworkdayjobs.com/careers",
    )
    by_query = canonical_job_identity(
        "https://acme.wd3.myworkdayjobs.com/es-419/careers/job/Madrid/Analista?jobid=55555",
        source_type="workday",
        source_key="workday:acme.wd3.myworkdayjobs.com/careers",
    )

    assert analyst.identity_key == "url://acme.wd3.myworkdayjobs.com/es-419/careers/job/Madrid/Analista"
    assert scientist.identity_key == "url://acme.wd3.myworkdayjobs.com/es-419/careers/job/Madrid/Cientifico"
    assert analyst.identity_key != scientist.identity_key
    assert requisition.identity_key == "workday:acme.wd3.myworkdayjobs.com:r-2048"
    assert by_query.identity_key == "workday:acme.wd3.myworkdayjobs.com:55555"


def test_ashby_and_workable_canonical_identities():
    a1 = canonical_job_identity("https://jobs.ashbyhq.com/ElevenLabs/b8a111fa-9fd5-44d0-97ed-8fd1280be4ef")
    a2 = canonical_job_identity("https://jobs.ashbyhq.com/elevenlabs/b8a111fa-9fd5-44d0-97ed-8fd1280be4ef?utm_source=x")
    assert a1.identity_key == a2.identity_key == "ashby:elevenlabs:b8a111fa-9fd5-44d0-97ed-8fd1280be4ef"

    w1 = canonical_job_identity("https://apply.workable.com/huggingface/j/9E2A4C02C7/")
    w2 = canonical_job_identity("https://apply.workable.com/j/9E2A4C02C7", source_type="workable", source_key="workable:huggingface")
    assert w1.identity_key == w2.identity_key == "workable:huggingface:9e2a4c02c7"


@pytest.mark.parametrize(
    "key",
    [
        "lever-origin",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "hsa_cam",
        "msclkid",
        "gbraid",
        "wbraid",
        "dclid",
        "yclid",
        "ttclid",
        "twclid",
        "li_fat_id",
        "igshid",
        "mkt_tok",
        "_gl",
        "mtm_campaign",
        "pk_campaign",
        "pk_cpn",
    ],
)
def test_common_tracking_keys_are_ignored(key):
    assert job_identity._is_tracking_key(key)


@pytest.mark.parametrize("key", ["token", "gh_jid", "ashby_jid", "req", "jobid", "id", "page", "pk_jobid"])
def test_job_keys_are_not_tracking(key):
    assert not job_identity._is_tracking_key(key)


def _cases() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("pair", _cases()["distinct_pairs"], ids=lambda pair: pair[1])
def test_distinct_vendor_jobs_keep_distinct_identities(pair):
    first, second = (canonical_job_identity(url).identity_key for url in pair)
    assert first != second


@pytest.mark.parametrize("url", _cases()["url_fallback_cases"])
def test_vendor_pages_without_a_job_id_keep_the_url_identity(url):
    assert canonical_job_identity(url).identity_key.startswith("url:")


@pytest.mark.parametrize("group", [g for g in _cases()["equivalent_groups"] if not g["source_key"]],
                         ids=lambda group: group["name"])
def test_vendor_identity_does_not_depend_on_source_info(group):
    # diagnose and web-search candidates compute identities without a source key.
    for url in group["urls"]:
        assert canonical_job_identity(url).identity_key == group["expected_identity_key"]
