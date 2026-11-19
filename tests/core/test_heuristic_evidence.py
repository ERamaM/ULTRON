"""Heuristic evidence fields only quote real, relevant source text (ported MARK II cases)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.models import ExtractedJob
from ultron.core.ranker.match_score import compute_match_score

F = Path(__file__).resolve().parents[1] / "fixtures"
PROFILE = yaml.safe_load((F / "profiles/ai-ml-remote-eu.yaml").read_text(encoding="utf-8"))
STRICT = {"seniority": {"required": True}, "remote_policy": {"required": "full_remote"}}


def _evaluate(job):
    return characterize_job(job, PROFILE, load_allowed_domains())["_evaluation"]


def _job(**overrides):
    base = {
        "apply_url": "https://example.org/job", "domain": "example.org", "title": "Senior ML Engineer",
        "apply_available": True,
    }
    return ExtractedJob(**{**base, **overrides})


def test_heuristic_evidence_only_uses_actual_relevant_terms():
    job = ExtractedJob(apply_url="https://example.org/job", domain="example.org", title="ML Engineer", body_text="Build systems.")
    r = _evaluate(job)
    assert r.seniority_evidence == ""
    assert r.remote_evidence == ""
    assert _evaluate(job.model_copy(update={"title": "Junior ML Engineer"})).seniority_evidence == "Junior ML Engineer"
    assert _evaluate(job.model_copy(update={"body_text": "Hybrid, 3 days in office."})).remote_evidence == "Hybrid, 3 days in office."


def test_ambiguous_seniority_corpus_case_stays_reviewable():
    corpus = json.loads((F / "heuristic_characterization_corpus.json").read_text(encoding="utf-8"))
    case = next(c for c in corpus["cases"] if c["id"] == "q-seniority-ambiguous")
    job = ExtractedJob(**{**corpus["defaults"], **case["job"], "apply_url": "https://boards.greenhouse.io/example/jobs/x"})
    r = _evaluate(job)
    assert r.verdict == "unsure"
    assert compute_match_score(r, STRICT, source=job).band != "ineligible"


@pytest.mark.parametrize("body", ["Trabajo híbrido, tres días en oficina.", "Puestos presenciales."])
def test_spanish_hybrid_evidence_preserves_explicit_contradiction(body):
    job = _job(body_text=body)
    r = _evaluate(job)
    assert r.verdict == "discard"
    assert r.is_full_remote is False
    assert r.remote_evidence == body
    assert compute_match_score(r, STRICT, source=job).dimensions["remote"].state == "missing"


def test_spanish_remote_terms_do_not_match_inside_other_words():
    assert _evaluate(_job(body_text="Estudiamos la hibridación y la presencialidad.")).remote_evidence == ""


def test_explicit_remote_field_remains_separate_from_following_body():
    job = _job(remote_text="Hybrid, 3 days in office", body_text="Build machine learning systems. Applications open.")
    r = _evaluate(job)
    assert r.remote_evidence == "Hybrid, 3 days in office"
    assert compute_match_score(r, STRICT, source=job).dimensions["remote"].state == "unknown"


def test_remote_evidence_uses_body_when_remote_field_is_generic():
    job = _job(remote_text="Flexible schedule", body_text="Hybrid, 3 days in office. Build ML systems.")
    r = _evaluate(job)
    assert r.remote_evidence == "Hybrid, 3 days in office."
    assert compute_match_score(r, STRICT, source=job).dimensions["remote"].state == "unknown"


def test_mixed_remote_evidence_is_not_conclusive():
    job = _job(remote_text="Remote", body_text="Hybrid, 3 days in office. Build ML systems.")
    r = _evaluate(job)
    assert r.is_full_remote is False
    assert r.remote_evidence == "Remote"
    assert compute_match_score(r, STRICT, source=job).dimensions["remote"].state == "unknown"
