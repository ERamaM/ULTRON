"""finalize: profile-conditioned location review of heuristic keeps (owner request of 2026-11-20)."""

from __future__ import annotations

import json

from stage_helpers import PROFILE

from ultron.cli import main
from ultron.profiles import store as profiles
from ultron.storage import runs

ML_BODY = "Build machine learning services, LLM evaluation and model serving. Applications are open."


def _run(capsys, postings, profile=PROFILE):
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    run = runs.load_run(run_id)
    run["profile_snapshot"] = profile
    runs._write_run(run_id, run)
    rows = []
    for index, (title, location, remote, body, *company) in enumerate(postings):
        job = {
            "title": title, "company": company[0] if company else f"Synthetic Employer {index}",
            "location": location, "remote_text": remote, "body_text": body, "apply_url": f"https://boards.greenhouse.io/example/jobs/{index}",
            "domain": "boards.greenhouse.io", "apply_available": True, "is_open_hint": True,
        }
        candidate = {
            "query_id": "", "query": "", "source_url": job["apply_url"], "normalized_url": job["apply_url"],
            "domain": "boards.greenhouse.io", "source_type": "ats_api", "source_key": "greenhouse:example",
        }
        rows.append({"candidate": candidate, "job": job, "identity_key": f"id:{index}"})
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


def test_the_body_opens_a_remote_job_but_not_an_onsite_one(capsys):
    profile = json.loads(json.dumps(PROFILE))
    profile["criteria"]["remote_policy"] = {"required": "any"}
    europe = " Our teams work across Europe."
    rows = _run(capsys, [
        ("Senior Machine Learning Engineer", "Lyon, France", "On-site", ML_BODY + europe),
        ("Senior Machine Learning Engineer", "Berlin, Germany", "Fully remote", ML_BODY + europe),
    ], profile)
    assert rows["Lyon, France"]["evaluation_verdict"] == "unsure"
    assert "france" in rows["Lyon, France"]["eligibility_evidence"].lower()
    assert rows["Berlin, Germany"]["evaluation_verdict"] == "keep"


def test_a_us_state_licence_limits_an_onsite_job_that_names_no_country(capsys):
    profile = json.loads(json.dumps(PROFILE))
    profile["criteria"]["remote_policy"] = {"required": "any"}
    licence = " Current state licensure and compliance with federal and state regulations are required."
    rows = _run(capsys, [
        ("Senior Machine Learning Engineer", "Riverside Campus", "On-site", ML_BODY + licence),
        ("Senior Machine Learning Engineer", "Valencia", "On-site", ML_BODY),
    ], profile)
    assert rows["Riverside Campus"]["evaluation_verdict"] == "unsure"
    assert "united states" in rows["Riverside Campus"]["eligibility_evidence"].lower()
    assert rows["Valencia"]["evaluation_verdict"] == "keep"


def test_a_remote_profile_sees_one_row_for_a_posting_listed_per_city(capsys):
    title = "Senior Machine Learning Engineer - Remote EU"
    rows = _run(capsys, [
        (title, "Madrid, Spain", "Remote within the EU", ML_BODY, "Synthetic Employer"),
        (title, "Lisbon, Portugal", "Remote within the EU", ML_BODY, "Synthetic Employer"),
    ])
    assert list(rows) == ["Madrid, Spain; Lisbon, Portugal"]


def test_an_onsite_profile_keeps_one_row_per_city(capsys):
    profile = json.loads(json.dumps(PROFILE))
    profile["criteria"]["remote_policy"] = {"required": "any"}
    title = "Senior Machine Learning Engineer"
    rows = _run(capsys, [
        (title, "Madrid, Spain", "On-site", ML_BODY, "Synthetic Employer"),
        (title, "Barcelona, Spain", "On-site", ML_BODY, "Synthetic Employer"),
    ], profile)
    assert sorted(rows) == ["Barcelona, Spain", "Madrid, Spain"]