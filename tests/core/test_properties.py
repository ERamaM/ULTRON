"""Deterministic property checks ported from the MARK II project: identity idempotence and match-score bounds.

The original used hypothesis; these use fixed grids and a seeded generator so every run checks the same cases.
"""

from __future__ import annotations

import itertools
import random

import pytest

from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import ExtractedJob
from ultron.core.ranker.match_score import compute_match_score

HOSTS = ["acme.com", "boards.greenhouse.io", "jobs.lever.co", "jobs.ashbyhq.com", "apply.workable.com",
         "acme.wd3.myworkdayjobs.com", "acme.jobs.personio.de", "x.example.org", "jobs.smartrecruiters.com",
         "acme.teamtailor.com", "acme.bamboohr.com", "careers-acme.icims.com", "jobs.jobvite.com"]
PATHS = ["", "/", "/acme/jobs/123", "/acme/11111111-2222-3333-4444-555555555555", "/acme/j/AB12CD34", "/job/987",
         "/en-US/External/job/Madrid/ML_R-2048", "/careers/a_b-c/", "/a//b/", "/Acme/744000012345678-ml",
         "/jobs/123456-ml-engineer", "/careers/42", "/jobs/1234/ml/job", "/acme/job/oAbC1fwX"]


@pytest.mark.parametrize("host,path", list(itertools.product(HOSTS, PATHS)))
def test_identity_idempotent_and_drops_tracking(host, path):
    rnd = random.Random(f"{host}{path}")  # seeded per case, so a single case reruns identically
    tracking = "".join(rnd.choice("abc123") for _ in range(rnd.randint(0, 8)))
    first = canonical_job_identity(f"HTTPS://{host.upper()}{path}?utm_source={tracking}#fragment")
    second = canonical_job_identity(first.canonical_url)
    assert second.canonical_url == first.canonical_url
    assert second.identity_key == first.identity_key
    assert "utm_source" not in first.canonical_url and "#" not in first.canonical_url


TRI = [None, True, False]
EVID = ["", "Hybrid, 2 days in office", "remote", "Senior", "x" * 20]


@pytest.mark.parametrize("role,senior,remote", list(itertools.product(TRI, TRI, TRI)))
@pytest.mark.parametrize("evidence", EVID)
def test_match_score_bounded_without_source(role, senior, remote, evidence):
    ev = {"is_allowed_family": role, "role_evidence": evidence, "is_senior": senior, "seniority_evidence": evidence,
          "is_full_remote": remote, "remote_evidence": evidence, "open_status_ok": True}
    score = compute_match_score(ev, {"seniority": {"required": True}, "remote_policy": {"required": "full_remote"}}, source=None)
    assert 0 <= score.value <= 100
    assert not score.hard_incompatibilities
    assert all(item.state != "missing" for item in score.dimensions.values())


@pytest.mark.parametrize("evidence", ["Hybrid, 2 days in office", "Onsite attendance is required."])
def test_verified_explicit_remote_contradictions_are_ineligible(evidence):
    job = ExtractedJob(apply_url="https://example.test/job", domain="example.test", remote_text=evidence)
    score = compute_match_score({"verdict": "discard", "is_full_remote": False, "remote_evidence": evidence},
                                {"remote_policy": {"required": "full_remote"}}, source=job)
    assert score.dimensions["remote"].state == "missing"
    assert score.band == "ineligible" and score.value == 0
