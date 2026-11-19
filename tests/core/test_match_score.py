from __future__ import annotations

import pytest

from ultron.core.models import ExtractedJob
from ultron.core.ranker.match_score import SCORE_VERSION
from ultron.core.ranker.match_score import compute_match_score as _compute_match_score


def compute_match_score(evaluation, criteria):
    """Use explicit synthetic source text for the score's provenance checks."""
    source = ExtractedJob(
        apply_url="https://example.test/job",
        domain="example.test",
        title=(evaluation.get("seniority_evidence") or "") if evaluation.get("is_senior") is False else "",
        remote_text=(evaluation.get("remote_evidence") or "") if evaluation.get("is_full_remote") is False else "",
        body_text="\n".join(
            str(evaluation.get(field) or "")
            for field, flag in (("role_evidence", "is_allowed_family"), ("open_status_evidence", "open_status_ok"))
            if evaluation.get(flag) is False
        ),
    )
    return _compute_match_score(evaluation, criteria, source=source)


STRICT = {
    "seniority": {"required": True, "preferred_terms": ["senior"]},
    "remote_policy": {"required": "full_remote"},
    "location_scope": {"preferred": ["Remote LATAM"]},
}


def _evaluation(**overrides):
    value = {
        "is_allowed_family": True,
        "role_evidence": "Backend role is explicit.",
        "is_senior": True,
        "seniority_evidence": "Senior in title.",
        "is_full_remote": True,
        "remote_evidence": "Remote LATAM explicit.",
        "eligibility": "high",
        "eligibility_evidence": "LATAM scope explicit.",
        "open_status_ok": True,
        "open_status_evidence": "Applications open.",
        "discard_category": None,
    }
    value.update(overrides)
    return value


def test_score_v1_strong_match_is_versioned_and_explainable():
    score = compute_match_score(_evaluation(), STRICT)
    assert score.value == 100
    assert score.band == "strong"
    assert score.version == SCORE_VERSION
    assert score.matched_requirements == ["role", "seniority", "remote", "eligibility", "open_status"]
    assert score.missing_requirements == []


def test_hard_incompatibility_cannot_be_compensated():
    score = compute_match_score(
        _evaluation(is_full_remote=False, remote_evidence="Three office days required."),
        STRICT,
    )
    assert score.value == 0
    assert score.band == "ineligible"
    assert score.hard_incompatibilities == ["remote_incompatible"]


def test_unknown_is_distinct_from_false_and_reaches_borderline():
    score = compute_match_score(
        _evaluation(is_senior=None, seniority_evidence="", eligibility=None, eligibility_evidence=""),
        STRICT,
    )
    assert score.dimensions["seniority"].state == "unknown"
    assert score.dimensions["eligibility"].state == "unknown"
    assert score.band in {"good", "borderline"}
    assert set(score.uncertain_requirements) == {"seniority", "eligibility"}


def test_absent_preferences_are_not_required_or_scored_as_failures():
    score = compute_match_score(
        _evaluation(is_senior=False, seniority_evidence="Junior", is_full_remote=False, remote_evidence="Onsite"),
        # A missing remote_policy.required defaults to full_remote, so opting out of remote must be explicit.
        {"remote_policy": {"required": "any"}},
    )
    assert score.dimensions["seniority"].state == "not_required"
    assert score.dimensions["remote"].state == "not_required"
    assert score.band == "strong"


def test_score_is_monotonic_for_an_isolated_unknown_to_match_improvement():
    unknown = compute_match_score(_evaluation(eligibility=None, eligibility_evidence=""), STRICT)
    matched = compute_match_score(_evaluation(), STRICT)
    assert 0 <= unknown.value < matched.value <= 100


def test_unsure_never_turns_false_evidenced_dimensions_into_incompatibility():
    score = compute_match_score(_evaluation(verdict="unsure", is_senior=False, seniority_evidence="Junior"), STRICT)
    assert score.dimensions["seniority"].state == "unknown"
    assert score.hard_incompatibilities == []
    assert score.band != "ineligible"


def test_confirmed_remote_contradiction_and_eligibility_levels():
    for verdict in ("keep", "discard"):
        score = compute_match_score(
            _evaluation(verdict=verdict, is_full_remote=False, remote_evidence="Hybrid, 3 days in office"), STRICT
        )
        assert score.dimensions["remote"].state == "missing"
        assert score.band == "ineligible"
    medium = compute_match_score(_evaluation(eligibility="medium", eligibility_evidence="Remote EMEA"), STRICT)
    assert medium.dimensions["eligibility"].state == "unknown"
    assert medium.dimensions["eligibility"].evidence == "Remote EMEA"
    assert compute_match_score(_evaluation(eligibility="high"), STRICT).dimensions["eligibility"].state == "matched"
    assert SCORE_VERSION == "score-v3"


@pytest.mark.parametrize(
    ("dimension", "value_field", "evidence_field", "evidence", "expected"),
    [
        ("remote", "is_full_remote", "remote_evidence", "Collaborate with remote teams.", "unknown"),
        ("remote", "is_full_remote", "remote_evidence", "Hybrid cloud architecture.", "unknown"),
        ("remote", "is_full_remote", "remote_evidence", "On-site work is optional.", "unknown"),
        ("remote", "is_full_remote", "remote_evidence", "Onsite attendance is not required.", "unknown"),
        ("remote", "is_full_remote", "remote_evidence", "Onsite attendance is required.", "missing"),
        ("remote", "is_full_remote", "remote_evidence", "Asistencia presencial obligatoria.", "missing"),
        ("remote", "is_full_remote", "remote_evidence", "Hybrid, 3 days in office", "missing"),
        ("remote", "is_full_remote", "remote_evidence", "Hybrid\n3 days in office.", "missing"),
        ("remote", "is_full_remote", "remote_evidence", "Mandatory hybrid working.", "missing"),
        ("remote", "is_full_remote", "remote_evidence", "Mandatory hybrid working is optional.", "unknown"),
        ("remote", "is_full_remote", "remote_evidence", "Hybrid, 0 days in office", "unknown"),
        ("remote", "is_full_remote", "remote_evidence", "Hybrid, 00 days in office", "unknown"),
        ("remote", "is_full_remote", "remote_evidence", "Puesto híbrido, 0 días en oficina.", "unknown"),
        ("remote", "is_full_remote", "remote_evidence", "Trabajo híbrido, tres días en oficina.", "missing"),
        ("remote", "is_full_remote", "remote_evidence", "Sin oficina; trabajo híbrido opcional.", "unknown"),
        ("seniority", "is_senior", "seniority_evidence", "Junior ML Engineer", "missing"),
        ("seniority", "is_senior", "seniority_evidence", "Ingeniero junior", "missing"),
        ("seniority", "is_senior", "seniority_evidence", "Not a junior engineer", "unknown"),
        ("seniority", "is_senior", "seniority_evidence", "Senior engineer mentoring junior engineers", "unknown"),
        ("role", "is_allowed_family", "role_evidence", "Build backend services.", "unknown"),
        ("open_status", "open_status_ok", "open_status_evidence", "Applications closed.", "missing"),
        ("open_status", "open_status_ok", "open_status_evidence", "Applications are not closed.", "unknown"),
        ("open_status", "open_status_ok", "open_status_evidence", "Funding round closed.", "unknown"),
        ("open_status", "open_status_ok", "open_status_evidence", "Vacante cerrada.", "missing"),
    ],
)
def test_only_explicit_dimension_specific_contradictions_are_missing(
    dimension, value_field, evidence_field, evidence, expected
):
    updates = {"verdict": "discard", value_field: False, evidence_field: evidence}
    if dimension == "open_status":
        updates["discard_category"] = "closed_or_invalid"
    score = compute_match_score(_evaluation(**updates), STRICT)
    assert score.dimensions[dimension].state == expected


def test_uncertainty_and_mixed_text_cannot_create_hard_contradictions():
    for evidence in ("", None, "Hybrid, 3 days in office.\nRemote is also possible."):
        result = compute_match_score(
            _evaluation(verdict="discard", is_full_remote=False, remote_evidence=evidence), STRICT
        )
        assert result.dimensions["remote"].state == "unknown"
    result = compute_match_score(
        _evaluation(verdict="unsure", is_full_remote=False, remote_evidence="Hybrid, 3 days in office"), STRICT
    )
    assert result.dimensions["remote"].state == "unknown"


def test_role_contradiction_requires_one_matching_profile_target():
    evaluation = _evaluation(
        verdict="discard", is_allowed_family=False, role_evidence="This is not a backend engineer role."
    )
    assert (
        compute_match_score(evaluation, {"role_focus_terms": ["backend engineer"]}).dimensions["role"].state
        == "missing"
    )
    assert (
        compute_match_score(evaluation, {"role_focus_terms": ["backend engineer", "data scientist"]})
        .dimensions["role"]
        .state
        == "unknown"
    )
    assert (
        compute_match_score(evaluation, {"role_focus_terms": ["backend engineer"], "target_titles": ["data scientist"]})
        .dimensions["role"]
        .state
        == "unknown"
    )
    assert compute_match_score(evaluation, {}).dimensions["role"].state == "unknown"
    spanish = _evaluation(verdict="discard", is_allowed_family=False, role_evidence="No es un puesto de ingeniero.")
    assert compute_match_score(spanish, {"role_focus_terms": ["ingeniero"]}).dimensions["role"].state == "missing"
    for evidence, target in (
        ("This is not not a backend engineer role.", "backend engineer"),
        ("This is not a backend engineer role. Backend engineer roles are available.", "backend engineer"),
        ("This is not a backendXengineer role.", "backend.engineer"),
    ):
        evaluation["role_evidence"] = evidence
        assert compute_match_score(evaluation, {"role_focus_terms": [target]}).dimensions["role"].state == "unknown"


@pytest.mark.parametrize("preferred", [None, [], ["junior"], ["mid"], ["senior", "junior"], "junior"])
def test_junior_evidence_does_not_contradict_other_seniority_preferences(preferred):
    evaluation = _evaluation(verdict="discard", is_senior=False, seniority_evidence="Junior Software Engineer")
    score = compute_match_score(evaluation, {"seniority": {"required": True, "preferred_terms": preferred}})
    assert score.dimensions["seniority"].state == "unknown"


@pytest.mark.parametrize(
    ("body", "evidence", "expected"),
    [
        ("APPLICATIONS CLOSED.", "Applications closed.", "missing"),
        ("Funding applications closed.", "Applications closed.", "unknown"),
        ("Applications are closed.", "Applications closed.", "unknown"),
        ("Applications closed? Apply today.", "Applications closed.", "unknown"),
        ("VACANTE CERRADA.\nOtra frase.", "vacante cerrada.", "unknown"),
        ("Applications closed!", "Applications closed.", "unknown"),
        ("Vacánte cerrada.", "Vacante cerrada.", "unknown"),
        ("Applications open.", "Applications closed.", "unknown"),
    ],
)
def test_contradiction_requires_full_source_sentence(body, evidence, expected):
    evaluation = _evaluation(
        verdict="discard", discard_category="closed_or_invalid", open_status_ok=False, open_status_evidence=evidence
    )
    job = ExtractedJob(apply_url="https://example.test/job", domain="example.test", body_text=body)
    score = _compute_match_score(evaluation, STRICT, source=job)
    assert score.dimensions["open_status"].state == expected


def test_contradiction_in_wrong_source_field_is_unknown():
    evaluation = _evaluation(verdict="discard", is_senior=False, seniority_evidence="Junior Engineer")
    job = ExtractedJob(
        apply_url="https://example.test/job",
        domain="example.test",
        title="Senior Engineer",
        body_text="Junior Engineer",
    )
    assert _compute_match_score(evaluation, STRICT, source=job).dimensions["seniority"].state == "unknown"
    assert _compute_match_score(evaluation, STRICT, source=None).dimensions["seniority"].state == "unknown"
    with pytest.raises(TypeError):
        _compute_match_score(evaluation, STRICT)


def test_source_proof_preserves_accent_and_ignores_case():
    evaluation = _evaluation(
        verdict="discard", is_full_remote=False, remote_evidence="Trabajo híbrido, tres días en oficina."
    )
    job = ExtractedJob(
        apply_url="https://example.test/job", domain="example.test", body_text="TRABAJO HÍBRIDO, TRES DÍAS EN OFICINA."
    )
    assert _compute_match_score(evaluation, STRICT, source=job).dimensions["remote"].state == "missing"


@pytest.mark.parametrize(
    ("dimension", "source_remote", "body", "evidence"),
    [
        (
            "open_status",
            "",
            "Applications closed. Applications have reopened and are currently open.",
            "Applications closed.",
        ),
        (
            "remote",
            "",
            "Hybrid, 3 days in office. This requirement no longer applies; fully remote is available.",
            "Hybrid, 3 days in office.",
        ),
        ("remote", "Hybrid, 3 days in office.", "No office attendance required.", "Hybrid, 3 days in office."),
        ("remote", "", "Hybrid, 3 days in office. Office attendance is not necessary.", "Hybrid, 3 days in office."),
        ("remote", "", "Hybrid, 3 days in office. Office attendance isn’t mandatory.", "Hybrid, 3 days in office."),
        ("open_status", "", "Applications closed. We are still accepting candidates.", "Applications closed."),
    ],
)
def test_source_with_contradictory_context_is_unknown(dimension, source_remote, body, evidence):
    values = {"verdict": "discard"}
    if dimension == "open_status":
        values.update(discard_category="closed_or_invalid", open_status_ok=False, open_status_evidence=evidence)
    else:
        values.update(is_full_remote=False, remote_evidence=evidence)
    job = ExtractedJob(
        apply_url="https://example.test/job", domain="example.test", remote_text=source_remote, body_text=body
    )
    assert _compute_match_score(_evaluation(**values), STRICT, source=job).dimensions[dimension].state == "unknown"


@pytest.mark.parametrize("extra", ["We build products.", "Office attendance is not necessary.", "Further terms apply."])
def test_uninterpreted_additional_context_is_unknown_and_retains_evidence(extra):
    evidence = "Hybrid, 3 days in office."
    evaluation = _evaluation(verdict="discard", is_full_remote=False, remote_evidence=evidence)
    for remote, body in ((evidence, extra), ("", evidence + " " + extra)):
        job = ExtractedJob(
            apply_url="https://example.test/job", domain="example.test", remote_text=remote, body_text=body
        )
        dimension = _compute_match_score(evaluation, STRICT, source=job).dimensions["remote"]
        assert dimension.state == "unknown"
        assert dimension.evidence == evidence
    complete = ExtractedJob(
        apply_url="https://example.test/job", domain="example.test", remote_text=evidence, body_text=evidence
    )
    assert _compute_match_score(evaluation, STRICT, source=complete).dimensions["remote"].state == "missing"
