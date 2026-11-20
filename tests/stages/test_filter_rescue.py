"""Borderline evaluator-stage discards go to the agent's ambiguous queue (bounded)."""

from __future__ import annotations

import json

import pytest
from stage_helpers import PROFILE

from ultron import profiles, runs
from ultron.cli import main
from ultron.stages import filter as filter_stage


def _job(n: int, **overrides) -> dict:
    job = {
        "title": "Machine Learning Engineer",
        "company": f"Acme {n}",
        "location": "Remote EU",
        "remote_text": "Remote EU",
        "body_text": "Build LLM retrieval and evaluation pipelines in production. Applications open.",
        "apply_url": f"https://boards.greenhouse.io/acme{n}/jobs/{n}",
        "domain": "boards.greenhouse.io",
        "apply_available": True,
    }
    job.update(overrides)
    return job


def _run(jobs: list[dict]) -> str:
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    run = runs.load_run(run_id)
    run["profile_snapshot"] = PROFILE
    runs._write_run(run_id, run)
    rows = [{"candidate": {}, "job": job, "identity_key": f"id:{n}"} for n, job in enumerate(jobs)]
    runs.write_jsonl(runs.run_dir(run_id) / "postings.jsonl", rows)
    runs.mark_stage(run_id, "fetch")
    return run_id


def _filter(capsys, run_id: str) -> dict:
    assert main(["filter", "--run", run_id]) == 0
    return json.loads(capsys.readouterr().out)


def test_borderline_heuristic_discard_goes_to_the_agent(capsys):
    run_id = _run([_job(0)])
    out = _filter(capsys, run_id)
    d = runs.run_dir(run_id)
    assert (out["discarded"], out["ambiguous"], out["rescued_discards"]) == (0, 1, 1)
    row = runs.read_jsonl(d / "ambiguous.jsonl")[0]
    assert row["rescued_discard"] == {"reason": "seniority_not_senior", "stage": "heuristic_evaluator"}
    assert row["heuristic"]["verdict"] == "discard"
    assert row["ambiguity"] >= runs.load_run(run_id)["settings"]["llm_ambiguity_threshold"]
    assert not runs.read_jsonl(d / "discarded.jsonl")
    assert main(["evaluate", "pending", "--run", run_id]) == 0
    item = json.loads(capsys.readouterr().out)["items"][0]
    assert item["heuristic"] == {"verdict": "discard", "discard_reason": "seniority_not_senior"}


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"remote_text": "Hybrid, 3 days in office"}, "hybrid_or_not_explicit_remote"),
        ({"body_text": "Build LLM pipelines. This position has been filled."}, "expired_or_closed"),
        ({"location": "Remote - United States", "remote_text": "Remote, US only"}, "non_eu_remote"),
    ],
)
def test_discards_with_hard_evidence_stay_final(capsys, overrides, reason):
    run_id = _run([_job(0, **overrides)])
    out = _filter(capsys, run_id)
    assert (out["ambiguous"], out["rescued_discards"]) == (0, 0)
    assert [row["reason"] for row in runs.read_jsonl(runs.run_dir(run_id) / "discarded.jsonl")] == [reason]


def test_low_ambiguity_heuristic_discard_stays_final(capsys):
    run_id = _run([_job(0)])
    run = runs.load_run(run_id)
    run["settings"]["llm_ambiguity_threshold"] = 100
    runs._write_run(run_id, run)
    out = _filter(capsys, run_id)
    assert (out["discarded"], out["rescued_discards"]) == (1, 0)
    assert runs.read_jsonl(runs.run_dir(run_id) / "discarded.jsonl")[0]["stage"] == "heuristic_evaluator"


def test_rescue_is_capped_and_keeps_the_highest_scores(capsys, monkeypatch):
    monkeypatch.setattr(filter_stage, "MAX_RESCUED_DISCARDS", 2)
    # The CET mention adds a time-zone signal, so job 3 outranks the others; ties keep file order.
    jobs = [_job(0), _job(1), _job(2), _job(3, body_text="Build LLM pipelines, CET hours. Applications open.")]
    run_id = _run(jobs)
    out = _filter(capsys, run_id)
    d = runs.run_dir(run_id)
    assert (out["ambiguous"], out["discarded"], out["rescued_discards"]) == (2, 2, 2)
    assert [row["identity_key"] for row in runs.read_jsonl(d / "ambiguous.jsonl")] == ["id:3", "id:0"]
    discarded = runs.read_jsonl(d / "discarded.jsonl")
    assert [row["identity_key"] for row in discarded] == ["id:1", "id:2"]
    assert out["discard_reasons"] == {"seniority_not_senior": 2}


def test_unevaluated_rescued_discard_is_discarded_at_finalize(capsys):
    run_id = _run([_job(0)])
    _filter(capsys, run_id)
    assert main(["finalize", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["roles"] == 0
    stage_discards = runs.read_jsonl(runs.run_dir(run_id) / "finalize_discards.jsonl")
    assert [(row["reason"], row["stage"]) for row in stage_discards] == [("seniority_not_senior", "evaluator")]


def test_agent_can_keep_a_rescued_discard(capsys, tmp_path):
    run_id = _run([_job(0)])
    _filter(capsys, run_id)
    evaluation = {"keep": True, "verdict": "keep", "is_senior": True, "is_allowed_family": True,
                  "is_full_remote": True, "eligibility": "high", "seniority_evidence": "6+ years shipping ML"}
    path = tmp_path / "ev.jsonl"
    path.write_text(json.dumps({"identity_key": "id:0", "evaluation": evaluation}) + "\n", encoding="utf-8")
    assert main(["evaluate", "record", "--run", run_id, "--from", str(path)]) == 0
    capsys.readouterr()
    assert main(["finalize", "--run", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["roles"] == 1


def test_pending_lists_rescued_discards_after_unsure_and_before_keeps(capsys):
    keep = _job(1, title="Senior Machine Learning Engineer")
    run_id = _run([keep, _job(0)])
    _filter(capsys, run_id)
    assert main(["evaluate", "pending", "--run", run_id]) == 0
    items = json.loads(capsys.readouterr().out)["items"]
    assert [item["heuristic"]["verdict"] for item in items] == ["discard", "keep"]


def test_a_posting_in_an_excluded_location_is_never_rescued():
    from types import SimpleNamespace

    from ultron.core.models import ExtractedJob

    result = {
        "pipeline_characterization": {"stage": "heuristic_evaluator"},
        "hard_filter": {"keep": True},
        # The evaluator reported seniority first; it never got to the location exclusion.
        "_evaluation": SimpleNamespace(open_status_ok=True, discard_category="seniority",
                                       discard_reason="seniority_not_senior"),
    }
    canada = ExtractedJob(**_job(0, location="Remote - Canada", remote_text="Remote within Canada"))
    assert filter_stage._rescuable(result, canada, ("canada",)) is False
    assert filter_stage._rescuable(result, canada, ()) is True  # no exclusion: rescued as before
    mexico = ExtractedJob(**_job(1, location="Remote - Mexico", remote_text="Remote within Mexico"))
    assert filter_stage._rescuable(result, mexico, ("canada",)) is True


def test_profile_explain_replay_reports_rescues_like_the_filter(capsys):
    from ultron import profile_explain

    run_id = _run([_job(0), _job(1, remote_text="Hybrid, 3 days in office")])
    out = _filter(capsys, run_id)
    replay = profile_explain.replay_run(PROFILE, run_id)
    assert (replay["kept"], replay["ambiguous"], replay["discarded"]) == (out["kept"], out["ambiguous"], out["discarded"])
    assert replay["rescued_discards"] == out["rescued_discards"] == 1
    assert replay["changes"] == {}  # same profile, same outcomes
