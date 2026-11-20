"""finalize: profile-conditioned location review of heuristic keeps (owner request of 2026-11-20)."""

from __future__ import annotations

import json

from stage_helpers import PROFILE

from ultron import profiles, runs
from ultron.cli import main

ML_BODY = "Build machine learning services, LLM evaluation and model serving. Applications are open."


def _run(capsys, postings, profile=PROFILE):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    run = runs.load_run(run_id)
    run["profile_snapshot"] = profile
    runs._write_run(run_id, run)
    rows = []
    for index, (title, location, remote, body) in enumerate(postings):
        job = {
            "title": title, "company": f"Synthetic Employer {index}", "location": location, "remote_text": remote,
            "body_text": body, "apply_url": f"https://boards.greenhouse.io/example/jobs/{index}",
            "domain": "boards.greenhouse.io", "apply_available": True, "is_open_hint": True,
        }
        rows.append({"candidate": {}, "job": job, "identity_key": f"id:{index}"})
    runs.write_jsonl(runs.run_dir(run_id) / "postings.jsonl", rows)
    runs.mark_stage(run_id, "fetch")
    assert main(["filter", "--run", run_id]) == 0
    assert main(["finalize", "--run", run_id]) == 0
    capsys.readouterr()
    payload = json.loads((runs.run_dir(run_id) / "results.json").read_text(encoding="utf-8"))
    return {row["location"]: row for row in payload["rows"]}


def test_a_remote_posting_limited_to_another_country_needs_review(capsys):
    rows = _run(capsys, [
        ("Senior Machine Learning Engineer", "Germany", "Remote across Germany", ML_BODY),
        ("Senior Machine Learning Engineer", "Spain", "Remote - Spain", ML_BODY),
    ])
    assert rows["Germany"]["evaluation_verdict"] == "unsure"
    assert rows["Germany"]["eligibility"] == "unknown"
    assert "germany" in rows["Germany"]["eligibility_evidence"].lower()
    assert rows["Spain"]["evaluation_verdict"] == "keep"
    assert rows["Germany"]["match_score"] < rows["Spain"]["match_score"]


def test_a_profile_without_a_home_country_keeps_single_country_postings(capsys):
    profile = json.loads(json.dumps(PROFILE))
    profile["criteria"]["location_scope"]["preferred"] = ["Remote EU", "Remote Europe"]
    rows = _run(capsys, [("Senior Machine Learning Engineer", "Germany", "Remote across Germany", ML_BODY)], profile)
    assert rows["Germany"]["evaluation_verdict"] == "keep"
