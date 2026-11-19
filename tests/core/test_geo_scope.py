"""Geography follows the profile scope: European rules only for European-scope profiles."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import yaml

from ultron import profiles, runs
from ultron.cli import main
from ultron.core.decision.decision import DecisionFinalizer
from ultron.core.decision.heuristics import HeuristicConfig
from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.final_row import prepare_geography
from ultron.core.models import ExtractedJob, JobEvaluation
from ultron.core.rules.geography import geo_scope, scoped_eligibility_terms
from ultron.core.rules.hard_filters import classify_remote_policy
from ultron.core.rules.role_matching import DEFAULT_ROLE_NEGATIVES, role_negatives_for

PROFILES = Path(__file__).resolve().parents[1] / "fixtures" / "profiles"


def _profile(slug: str) -> dict:
    return yaml.safe_load((PROFILES / f"{slug}.yaml").read_text(encoding="utf-8"))


def _job(title: str = "Senior Backend Engineer", location: str = "Remote", body: str = "") -> ExtractedJob:
    return ExtractedJob(
        title=title,
        company="Example",
        location=location,
        remote_text="Fully remote",
        body_text=body or "Build APIs and distributed systems on PostgreSQL. Applications open.",
        apply_url="https://boards.greenhouse.io/example/jobs/1",
        domain="boards.greenhouse.io",
        apply_available=True,
        is_open_hint=True,
    )


def _with_location_scope(slug: str, **location_scope) -> dict:
    profile = copy.deepcopy(_profile(slug))
    profile["criteria"]["location_scope"] = {**profile["criteria"].get("location_scope", {}), **location_scope}
    return profile


def test_geo_scope_detection():
    assert geo_scope(_profile("ai-ml-remote-eu")) == "eu"
    assert geo_scope(_profile("finance-eu")) == "eu"
    assert geo_scope(_profile("backend-latam")) == "other"
    assert geo_scope(_with_location_scope("backend-latam", preferred=[])) == "global"
    no_scope = copy.deepcopy(_profile("backend-latam"))
    del no_scope["criteria"]["location_scope"]
    assert geo_scope(no_scope) == "global"
    assert geo_scope({}) == "global"


def test_latam_job_not_discarded_as_non_eu():
    profile = _profile("backend-latam")
    for geo in ("other", None):
        result = characterize_job(_job(), profile, load_allowed_domains(), geo=geo)
        assert result["pipeline_characterization"]["reason"] != "non_eu_remote"
        assert result["pipeline_characterization"]["outcome"] == "keep"
        assert result["heuristic_evaluator"]["eligibility"] is None
    brazil = characterize_job(_job(location="Remote - Brazil"), profile, load_allowed_domains())
    assert brazil["pipeline_characterization"]["outcome"] == "keep"


def test_latam_eligibility_terms_mark_high():
    profile = _with_location_scope("backend-latam", eligibility_terms=["argentina"])
    result = characterize_job(_job(location="Remote - Argentina"), profile, load_allowed_domains())
    assert result["heuristic_evaluator"]["eligibility"] == "high"


def test_us_eligibility_does_not_match_the_pronoun_or_incidental_prose():
    profile = _with_location_scope("backend-latam", preferred=["United States"], eligibility_terms=["us"])
    job = _job(location="Prague, Czechia", body="Come work with us in our Prague office. We serve United States customers.")
    result = characterize_job(job, profile, load_allowed_domains())
    assert result["classification_signals"]["eligibility_hint"] is None
    assert result["heuristic_evaluator"]["eligibility"] is None
    assert prepare_geography(job, profile["criteria"], geo="other")[0].eligibility is None

    us_job = _job(location="Remote", body="This role is open to candidates in the US.")
    result = characterize_job(us_job, profile, load_allowed_domains())
    assert result["classification_signals"]["eligibility_hint"] == "high"
    assert result["heuristic_evaluator"]["eligibility"] == "high"


def test_preferred_regions_count_as_eligibility_terms():
    assert scoped_eligibility_terms(_profile("backend-latam")) == ("latam", "brazil", "mexico")
    generic = _with_location_scope("backend-latam", preferred=["Remote", "Worldwide", "Anywhere", "Global", "Remote Chile"])
    assert scoped_eligibility_terms(generic) == ("chile",)
    job = _job(body="Build APIs on PostgreSQL. We are open to candidates in Brazil. Applications open.")
    result = characterize_job(job, _profile("backend-latam"), load_allowed_domains())
    assert result["classification_signals"]["eligibility_hint"] == "high"
    assert result["heuristic_evaluator"]["eligibility"] == "high"
    assert result["pipeline_characterization"]["outcome"] == "keep"


def test_excluded_location_discards():
    profile = _with_location_scope("backend-latam", excluded_terms=["us only"])
    job = _job(location="Remote", body="Build APIs on PostgreSQL. Applications open.\nUS only")
    result = characterize_job(job, profile, load_allowed_domains())
    assert result["pipeline_characterization"] == {
        "outcome": "discard",
        "reason": "location_excluded",
        "stage": "heuristic_evaluator",
    }
    assert result["heuristic_evaluator"]["discard_category"] == "eligibility_scope"


def test_excluded_terms_do_not_change_european_scope():
    profile = _profile("ai-ml-remote-eu")
    job = _job(title="Senior ML Engineer", location="Remote EU", body="Build LLM systems. Applications open.\nUS only")
    assert characterize_job(job, profile, load_allowed_domains())["pipeline_characterization"]["reason"] != (
        "location_excluded"
    )


def test_global_scope_no_geo_filter():
    profile = _with_location_scope("backend-latam", preferred=[])
    us_job = _job(location="Remote - United States", body="Build APIs. Must be based in the US. Applications open.")
    result = characterize_job(us_job, profile, load_allowed_domains())
    assert result["pipeline_characterization"]["outcome"] == "keep"
    assert result["heuristic_evaluator"]["eligibility"] is None
    assert result["classification_signals"]["eligibility_hint"] is None
    # European rules would have marked this "high"; a global scope has no European rules.
    eu_job = _job(location="Remote EU", body="Build APIs. Fully remote within the EU. Applications open.")
    eu_result = characterize_job(eu_job, profile, load_allowed_domains())
    assert eu_result["classification_signals"]["eligibility_hint"] is None
    assert eu_result["heuristic_evaluator"]["eligibility"] is None
    assert eu_result["classification_signals"]["remote_scope_hint"] == "remote_eu"


def test_remote_other_only_outside_european_scope():
    text = "Remote - Brazil, fully remote"
    split = {"remote_text": "Fully remote", "body_text": "Build APIs."}
    assert classify_remote_policy(text, **split) is None
    assert classify_remote_policy(text, geo="eu", **split) is None
    assert classify_remote_policy(text, geo="other", **split) == "remote_other"
    assert classify_remote_policy(text, geo="global", **split) == "remote_other"
    assert classify_remote_policy("Remote EU", geo="other", remote_text="Remote EU") == "remote_eu"
    hybrid = {"remote_text": "Hybrid, 3 days in office", "body_text": ""}
    assert classify_remote_policy("Hybrid, 3 days in office", geo="other", **hybrid) is None


def test_remote_other_uses_split_fields_not_merged_text():
    # "hybrid cloud" in the body is a technology, not a work arrangement.
    body = "Location: Brazil. Build our hybrid cloud platform."
    merged = f"Remote - Brazil\nRemote\n{body}"
    assert classify_remote_policy(merged, geo="other", remote_text="Remote", body_text=body) == "remote_other"


def test_finance_negatives_unlock_by_prefix_without_role_signal_terms():
    profile = _profile("finance-eu")
    assert "role_signal_terms" not in profile["criteria"]
    negatives = role_negatives_for(profile)
    assert "finance" not in negatives.title_terms and "account" not in negatives.title_terms
    assert "sales" in negatives.title_terms
    assert role_negatives_for(_profile("ai-ml-remote-eu")) == DEFAULT_ROLE_NEGATIVES


def test_senior_financial_controller_keeps_end_to_end():
    job = _job(
        title="Senior Financial Controller",
        location="Remote",
        body="Own the month-end close, IFRS reporting and group consolidation. Fully remote within the EU. "
        "Applications open.",
    )
    job = job.model_copy(update={"remote_text": "Fully remote within the EU"})
    result = characterize_job(job, _profile("finance-eu"), load_allowed_domains())
    assert result["pipeline_characterization"] == {"outcome": "keep", "reason": None, "stage": "completed"}


def test_agent_eligibility_discard_is_neutral_outside_eu():
    def _discard(geo):
        evaluation = JobEvaluation(
            keep=False, verdict="discard", discard_category="eligibility_scope", is_senior=True,
            is_allowed_family=True, is_full_remote=True,
        )
        finalizer = DecisionFinalizer(HeuristicConfig(geo=geo), is_llama_cpp=lambda: False)
        return finalizer.finalize(evaluation, from_llm=True).discard_reason

    assert _discard("other") == "location_ineligible"
    assert _discard("global") == "location_ineligible"
    assert _discard("eu") == "missing_eu_spain_eligibility"
    assert _discard(None) == "missing_eu_spain_eligibility"


def test_finance_title_survives_prefilter_only_for_finance_profile():
    job = _job(title="Finance Controller", location="Remote", body="Own the month-end close. Applications open.")
    finance = characterize_job(job, _profile("finance-eu"), load_allowed_domains())
    assert finance["pre_filter"]["reason"] != "non_matching_role_family"
    ai = characterize_job(job, _profile("ai-ml-remote-eu"), load_allowed_domains())
    assert ai["pre_filter"] == {"keep": False, "reason": "non_matching_role_family"}


# --- stage level -------------------------------------------------------------------------


def _latam_run(jobs: list[ExtractedJob], profile: dict) -> str:
    """A fetched run; the ambiguity threshold is disabled so only the geography rule routes to the agent."""
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    run = runs.load_run(run_id)
    run["profile_snapshot"] = profile
    run["settings"]["llm_ambiguity_threshold"] = 101
    runs._write_run(run_id, run)
    rows = [
        {"candidate": {}, "job": job.model_dump(mode="json"), "identity_key": f"id:{index}"}
        for index, job in enumerate(jobs)
    ]
    runs.write_jsonl(runs.run_dir(run_id) / "postings.jsonl", rows)
    runs.mark_stage(run_id, "fetch")
    return run_id


def test_latam_unclear_eligibility_goes_to_agent(capsys):
    run_id = _latam_run([_job()], _profile("backend-latam"))
    assert main(["filter", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ambiguous"] == 1 and out["kept"] == 0, out


def test_latam_clear_eligibility_is_kept(capsys):
    job = _job(body="Build APIs on PostgreSQL. We are open to candidates in Brazil. Applications open.")
    run_id = _latam_run([job], _profile("backend-latam"))
    assert main(["filter", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kept"] == 1, out


def test_latam_row_finalizes_with_remote_other(capsys):
    run_id = _latam_run([_job(location="Remote - Brazil")], _profile("backend-latam"))
    assert main(["filter", "--run", run_id]) == 0
    capsys.readouterr()
    assert main(["finalize", "--run", run_id]) == 0
    capsys.readouterr()
    results = json.loads((runs.run_dir(run_id) / "results.json").read_text(encoding="utf-8"))
    assert [d for d in results["discards"] if d["reason"] == "non_eu_remote"] == []
    [row] = results["rows"]
    assert row["remote_policy"] == "remote_other"
    assert row["eligibility"] == "high"
    assert row["match_band"] in {"strong", "good"}
    markdown = (runs.run_dir(run_id) / "results.md").read_text(encoding="utf-8")
    assert "remote_other" in markdown


def test_latam_unknown_eligibility_finalizes_without_inventing_eu(capsys):
    run_id = _latam_run([_job()], _profile("backend-latam"))
    assert main(["filter", "--run", run_id]) == 0
    capsys.readouterr()
    assert main(["finalize", "--run", run_id]) == 0
    capsys.readouterr()
    results = json.loads((runs.run_dir(run_id) / "results.json").read_text(encoding="utf-8"))
    [row] = results["rows"]
    assert row["remote_policy"] == "remote_other"
    assert row["eligibility"] == "unknown"


def test_hybrid_cloud_body_keeps_agent_eligibility_with_remote_other(capsys, tmp_path):
    job = _job(body="Build our hybrid cloud platform on PostgreSQL. Applications open.")
    job = job.model_copy(update={"remote_text": "Remote"})
    run_id = _latam_run([job], _profile("backend-latam"))
    assert main(["filter", "--run", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["ambiguous"] == 1
    evaluation = {
        "keep": True, "verdict": "keep", "is_senior": True, "is_allowed_family": True, "is_full_remote": True,
        "eligibility": "high", "eligibility_evidence": "Agent: hires in Brazil",
    }
    path = tmp_path / "ev.jsonl"
    path.write_text(json.dumps({"identity_key": "id:0", "evaluation": evaluation}) + "\n", encoding="utf-8")
    assert main(["evaluate", "record", "--run", run_id, "--from", str(path)]) == 0
    capsys.readouterr()
    assert main(["finalize", "--run", run_id]) == 0
    capsys.readouterr()
    [row] = json.loads((runs.run_dir(run_id) / "results.json").read_text(encoding="utf-8"))["rows"]
    assert (row["remote_policy"], row["eligibility"]) == ("remote_other", "high")


def test_agent_eligibility_discard_on_latam_run_is_neutral(capsys, tmp_path):
    run_id = _latam_run([_job()], _profile("backend-latam"))
    assert main(["filter", "--run", run_id]) == 0
    capsys.readouterr()
    evaluation = {
        "keep": False, "verdict": "discard", "discard_category": "eligibility_scope", "is_senior": True,
        "is_allowed_family": True, "is_full_remote": True,
    }
    path = tmp_path / "ev.jsonl"
    path.write_text(json.dumps({"identity_key": "id:0", "evaluation": evaluation}) + "\n", encoding="utf-8")
    assert main(["evaluate", "record", "--run", run_id, "--from", str(path)]) == 0
    capsys.readouterr()
    assert main(["finalize", "--run", run_id]) == 0
    capsys.readouterr()
    reasons = [d["reason"] for d in json.loads((runs.run_dir(run_id) / "results.json").read_text(encoding="utf-8"))["discards"]]
    assert reasons == ["location_ineligible"]
