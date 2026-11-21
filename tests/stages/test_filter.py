from __future__ import annotations

import json

from stage_helpers import GOLDEN, make_run

from ultron.cli import main
from ultron.profiles import store as profiles
from ultron.storage import runs

# Reviewed deviations from the MARK III golden (see INTENTIONAL_DEVIATIONS in tests/core/test_parity_heuristics.py).
DEVIATED_OUTCOMES = {"es-positive-shape-01": {"outcome": "keep", "reason": None, "stage": "completed"}}


def _expected(cid: str) -> dict:
    return DEVIATED_OUTCOMES.get(cid) or GOLDEN[cid]["pipeline_characterization"]


def test_filter_matches_parity_semantics(capsys):
    ids = ["en-positive-shape-01", "en-negative-shape-02", "en-ambiguous-shape-01", "es-positive-shape-01"]
    run_id = make_run(ids)
    assert main(["filter", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    d = runs.run_dir(run_id)
    where = {}
    for name in ("kept", "ambiguous", "discarded"):
        for row in runs.read_jsonl(d / f"{name}.jsonl"):
            where[row["identity_key"]] = (name, row)
    assert len(where) == 4
    assert out["kept"] + out["ambiguous"] + out["discarded"] == 4
    for cid in ids:
        outcome = _expected(cid)["outcome"]
        name, row = where[f"id:{cid}"]
        if outcome == "discard":
            assert name == "discarded"
            assert row["stage"] == _expected(cid)["stage"]
            assert row["reason"] == _expected(cid)["reason"]
        elif outcome == "review":
            assert name == "ambiguous"
        else:
            assert name in ("kept", "ambiguous")
    assert sum(out["discard_reasons"].values()) == out["discarded"]
    assert "evaluate" not in runs.load_run(run_id)["stages_done"]
    assert "filter" in runs.load_run(run_id)["stages_done"]


def test_filter_requires_fetch(capsys):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    assert main(["filter", "--run", run_id]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "stage_order"


def test_filter_discards_us_only_and_remote_reimbursement_for_eu_profile(capsys):
    from ultron.core.models import ExtractedJob

    assert main(["run", "new", "--profile", "ai-ml-remote-eu", "--preset", "fast"]) == 0
    run_id = json.loads(capsys.readouterr().out)["run_id"]
    d = runs.run_dir(run_id)
    us_job = ExtractedJob(
        title="Senior Machine Learning Engineer",
        company="Nebius",
        location="Palo Alto, California, United States",
        remote_text="Remote work reimbursement: up to $85/month for mobile and internet.",
        body_text=(
            "Headquartered in Amsterdam with R&D hubs across Europe and North America. "
            "Build LLM inference systems. Remote work reimbursement: up to $85/month."
        ),
        apply_url="https://boards.greenhouse.io/nebius/jobs/1",
        domain="boards.greenhouse.io",
    )
    ny_remote = ExtractedJob(
        title="Senior Machine Learning Engineer",
        company="Spotify",
        location="New York, NY",
        remote_text="Remote",
        body_text="Build recommendation ML models at scale in Python and PyTorch.",
        apply_url="https://jobs.lever.co/spotify/11111111-1111-1111-1111-111111111111",
        domain="jobs.lever.co",
    )
    eu_job = ExtractedJob(
        title="Senior Machine Learning Engineer",
        company="Nebius",
        location="Remote - Europe",
        remote_text="Remote - Europe",
        body_text="Build LLM inference optimization and serving systems. Remote - Europe.",
        apply_url="https://boards.greenhouse.io/nebius/jobs/2",
        domain="boards.greenhouse.io",
    )
    runs.write_jsonl(d / "postings.jsonl", [
        {"candidate": {"source_type": "ats_api"}, "job": us_job.model_dump(mode="json"), "identity_key": "greenhouse:nebius:1", "ats_fingerprint": True},
        {"candidate": {"source_type": "ats_api"}, "job": ny_remote.model_dump(mode="json"), "identity_key": "lever:spotify:11111111-1111-1111-1111-111111111111", "ats_fingerprint": True},
        {"candidate": {"source_type": "ats_api"}, "job": eu_job.model_dump(mode="json"), "identity_key": "greenhouse:nebius:2", "ats_fingerprint": True},
    ])
    runs.mark_stage(run_id, "fetch")
    assert main(["filter", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ambiguous"] == 1 and out["discarded"] == 2
    assert runs.read_jsonl(d / "ambiguous.jsonl")[0]["identity_key"] == "greenhouse:nebius:2"
