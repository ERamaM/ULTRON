from __future__ import annotations

import copy
from pathlib import Path

import yaml

from ultron.core.decision.evaluation_profile import EvaluationProfile
from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.models import ExtractedJob
from ultron.core.ranker.match_score import compute_match_score
from ultron.core.remote_requirement import DEFAULT_REMOTE_REQUIRED, is_remote_required, remote_required_value

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
PROFILE = yaml.safe_load((FIXTURES / "profiles" / "ai-ml-remote-eu.yaml").read_text(encoding="utf-8"))


def _profile_without_remote_policy() -> dict:
    profile = copy.deepcopy(PROFILE)
    profile["criteria"].pop("remote_policy", None)
    return profile


def test_missing_remote_policy_defaults_to_full_remote_in_evaluation_profile():
    assert EvaluationProfile.from_search_profile(_profile_without_remote_policy()).remote_policy == "full_remote"


def test_missing_remote_policy_requires_remote_in_match_score():
    evaluation = {"is_full_remote": True, "remote_evidence": "Fully remote."}
    score = compute_match_score(evaluation, {}, source=None)
    assert score.dimensions["remote"].state != "not_required"


def test_missing_remote_policy_requires_remote_in_characterization():
    job = ExtractedJob(
        apply_url="https://boards.greenhouse.io/example/jobs/1",
        domain="boards.greenhouse.io",
        title="Senior ML Engineer",
        company="Example",
        location="Madrid, Spain",
        remote_text="Hybrid",
        body_text="Senior ML Engineer building LLM systems. Hybrid role with 3 days per week in our Madrid office.",
        apply_available=True,
        is_open_hint=True,
    )
    without_key = characterize_job(job, _profile_without_remote_policy(), load_allowed_domains())
    explicit = characterize_job(job, PROFILE, load_allowed_domains())
    for key in ("pre_filter", "heuristic_evaluator", "hard_filter"):
        assert without_key[key] == explicit[key], key
    assert without_key["heuristic_evaluator"]["is_full_remote"] is False
    assert without_key["heuristic_evaluator"]["discard_reason"] is not None


def test_remote_requirement_helper_default_and_opt_out():
    # final_row and the discover/feeds stages call this helper directly.
    assert DEFAULT_REMOTE_REQUIRED == "full_remote"
    for criteria in (None, {}, {"remote_policy": {}}, {"remote_policy": {"required": None}}):
        assert remote_required_value(criteria) == "full_remote"
        assert is_remote_required(criteria) is True
    assert is_remote_required({"remote_policy": {"required": "any"}}) is False
    assert is_remote_required({"remote_policy": {"required": "Full_Remote"}}) is True
