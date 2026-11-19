from __future__ import annotations

import json

from stage_helpers import make_run

from ultron import ledger, runs
from ultron.cli import main

KEEP = ["en-positive-shape-01", "en-positive-shape-02", "en-positive-shape-03"]
AMBIG = ["en-ambiguous-shape-01"]


def _filtered(capsys, cases, max_roles=None):
    run_id = make_run(cases)
    if max_roles is not None:
        run = runs.load_run(run_id)
        run["settings"]["max_roles"] = max_roles
        runs._write_run(run_id, run)
    assert main(["filter", "--run", run_id]) == 0
    capsys.readouterr()
    return run_id


def _results(run_id):
    return json.loads((runs.run_dir(run_id) / "results.json").read_text(encoding="utf-8"))


def test_agent_evaluation_overrides_heuristic(capsys, tmp_path):
    run_id = _filtered(capsys, AMBIG)
    d = runs.run_dir(run_id)
    ambiguous = runs.read_jsonl(d / "ambiguous.jsonl")
    assert ambiguous
    key = ambiguous[0]["identity_key"]
    ev = {"keep": True, "verdict": "keep", "is_senior": True, "is_allowed_family": True, "is_full_remote": True,
          "fit_signals": ["agent says fit"]}
    p = tmp_path / "ev.jsonl"
    p.write_text(json.dumps({"identity_key": key, "evaluation": ev}) + "\n", encoding="utf-8")
    assert main(["evaluate", "record", "--run", run_id, "--from", str(p)]) == 0
    capsys.readouterr()
    assert main(["finalize", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["roles"] >= 1
    rows = _results(run_id)["rows"]
    assert any(r["evaluation_verdict"] == "keep" and "agent says fit" in r["fit_signals"] for r in rows)


def test_ambiguous_without_evaluation_stays_unsure(capsys):
    run_id = _filtered(capsys, AMBIG)
    assert main(["finalize", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["unsure"] >= 1
    assert all(r["evaluation_verdict"] == "unsure" for r in _results(run_id)["rows"])


def test_final_cap_recorded(capsys):
    run_id = _filtered(capsys, KEEP, max_roles=1)
    d = runs.run_dir(run_id)
    assert len(runs.read_jsonl(d / "kept.jsonl")) + len(runs.read_jsonl(d / "ambiguous.jsonl")) >= 2
    assert main(["finalize", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["roles"] == 1 and out["final_cap"] >= 1
    res = _results(run_id)
    assert len(res["rows"]) == 1
    assert sum(1 for d in res["discards"] if d["reason"] == "final_cap") == out["final_cap"]


def test_stale_evaluations_ignored(capsys):
    run_id = _filtered(capsys, KEEP[:1])
    d = runs.run_dir(run_id)
    ev = {"keep": False, "verdict": "discard", "discard_reason": "x", "is_senior": True,
          "is_allowed_family": True, "is_full_remote": True}
    runs.write_jsonl(d / "evaluations.jsonl", [{"identity_key": "id:gone", "evaluation": ev}])
    assert main(["finalize", "--run", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["roles"] == 1


def test_finalize_requires_filter(capsys):
    run_id = make_run(KEEP)
    assert main(["finalize", "--run", run_id]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "stage_order"


def test_finalize_twice_idempotent(capsys):
    from contextlib import closing

    run_id = _filtered(capsys, KEEP)
    assert main(["finalize", "--run", run_id]) == 0
    first = (runs.run_dir(run_id) / "results.json").read_bytes()
    capsys.readouterr()
    assert main(["finalize", "--run", run_id]) == 0
    assert (runs.run_dir(run_id) / "results.json").read_bytes() == first
    with closing(ledger.connect()) as conn:
        assert {r[0] for r in conn.execute("SELECT times_seen FROM jobs")} == {1}


def test_is_new_flag(capsys):
    first = _filtered(capsys, KEEP[:1])
    assert main(["finalize", "--run", first]) == 0
    assert all(r["is_new"] is True for r in _results(first)["rows"])
    capsys.readouterr()
    second = _filtered(capsys, KEEP[:1])
    assert main(["finalize", "--run", second]) == 0
    out = json.loads(capsys.readouterr().out)
    assert all(r["is_new"] is False for r in _results(second)["rows"])
    assert out["new_roles"] == 0 and out["top"][0]["is_new"] is False


def test_ambiguous_heuristic_keep_without_evaluation_stays_keep(capsys):
    run_id = _filtered(capsys, KEEP[:1])
    d = runs.run_dir(run_id)
    row = runs.read_jsonl(d / "ambiguous.jsonl")[0]
    assert row["heuristic"]["verdict"] == "keep"
    assert main(["finalize", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert [r["evaluation_verdict"] for r in _results(run_id)["rows"]] == ["keep"]
    assert out["agent_evaluated"] == 0 and out["heuristic_fallback"] == 1 and out["unsure"] == 0


def test_ambiguous_heuristic_unsure_without_evaluation_stays_unsure(capsys):
    run_id = _filtered(capsys, AMBIG)
    assert runs.read_jsonl(runs.run_dir(run_id) / "ambiguous.jsonl")[0]["heuristic"]["verdict"] == "unsure"
    assert main(["finalize", "--run", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["unsure"] == 1
    assert [r["evaluation_verdict"] for r in _results(run_id)["rows"]] == ["unsure"]


def test_finalize_discards_persisted_with_stage(capsys):
    run_id = _filtered(capsys, KEEP, max_roles=1)
    assert main(["finalize", "--run", run_id]) == 0
    out = json.loads(capsys.readouterr().out)
    path = runs.run_dir(run_id) / "finalize_discards.jsonl"
    assert out["outputs"]["finalize_discards_jsonl"] == str(path)
    rows = runs.read_jsonl(path)
    caps = [r for r in rows if r["stage"] == "final_cap"]
    assert len(caps) == out["final_cap"] >= 1
    assert all(r["identity_key"].startswith("id:") and r["reason"] == "final_cap" for r in caps)
    first = path.read_bytes()
    assert main(["finalize", "--run", run_id]) == 0
    assert path.read_bytes() == first


def test_duplicate_role_recorded_with_dedup_stage(capsys):
    run_id = _filtered(capsys, KEEP[:1])
    d = runs.run_dir(run_id)
    entry = runs.read_jsonl(d / "ambiguous.jsonl")[0]
    clone = {**entry, "identity_key": "id:dup-clone"}
    runs.write_jsonl(d / "ambiguous.jsonl", [entry, clone])
    assert main(["finalize", "--run", run_id]) == 0
    rows = runs.read_jsonl(d / "finalize_discards.jsonl")
    dups = [r for r in rows if r["stage"] == "dedup"]
    assert len(dups) == 1 and dups[0]["reason"] == "duplicate_role" and dups[0]["identity_key"].startswith("id:")


def test_finalize_again_keeps_novelty_and_reconciles_discard(capsys, tmp_path):
    from contextlib import closing

    first = _filtered(capsys, KEEP[:1])
    assert main(["finalize", "--run", first]) == 0
    assert json.loads(capsys.readouterr().out)["new_roles"] == 1
    second = _filtered(capsys, KEEP[:1])
    assert main(["finalize", "--run", second]) == 0
    assert json.loads(capsys.readouterr().out)["new_roles"] == 0

    assert main(["finalize", "--run", first]) == 0
    assert json.loads(capsys.readouterr().out)["new_roles"] == 1
    source = tmp_path / "discard.jsonl"
    runs.write_jsonl(source, [{
        "identity_key": f"id:{KEEP[0]}",
        "evaluation": {
            "keep": False, "verdict": "discard", "discard_reason": "role_mismatch",
            "is_senior": True, "is_allowed_family": False, "is_full_remote": True,
        },
    }])
    assert main(["evaluate", "record", "--run", first, "--from", str(source)]) == 0
    capsys.readouterr()
    assert main(["finalize", "--run", first]) == 0
    assert json.loads(capsys.readouterr().out)["roles"] == 0
    with closing(ledger.connect()) as conn:
        assert conn.execute("SELECT run_id FROM job_occurrences").fetchall() == [(second,)]
        assert conn.execute("SELECT times_seen, first_seen_run_id, last_seen_run_id FROM jobs").fetchall() == [
            (1, second, second)
        ]
    assert main(["finalize", "--run", second]) == 0
    assert json.loads(capsys.readouterr().out)["new_roles"] == 0


def test_geo_eu_handles_hyphenated_remote_europe_and_washington_dc():
    from ultron.core.rules.geo_eu import classify_eu_eligibility, classify_eu_remote_policy, has_region_red_flags

    assert classify_eu_remote_policy("Remote - Europe") == "remote_europe"
    assert classify_eu_eligibility("Remote - Europe") == "high"
    assert classify_eu_remote_policy("Remote - EU") == "remote_eu"
    assert has_region_red_flags("Washington, DC") is True


def test_finalize_populates_full_query_log_from_candidates(capsys):
    run_id = _filtered(capsys, KEEP[:1])
    d = runs.run_dir(run_id)
    postings = runs.read_jsonl(d / "postings.jsonl")
    postings[0]["candidate"] = {
        "query_id": "web:001", "query": "Senior ML Engineer Remote Europe",
        "source_url": postings[0]["job"]["apply_url"], "normalized_url": postings[0]["job"]["apply_url"],
        "domain": postings[0]["job"]["domain"], "source_type": "web_search", "source_key": "web_search",
    }
    runs.write_jsonl(d / "postings.jsonl", postings)
    assert main(["finalize", "--run", run_id]) == 0
    capsys.readouterr()
    summary_md = (d / "run_summary.md").read_text(encoding="utf-8")
    assert "web:001: Senior ML Engineer Remote Europe (hits=1)" in summary_md


def test_finalize_does_not_invent_remote_spain_when_remote_not_required():
    from ultron.core.final_row import _assemble_final_row, resolve_geo_defaults
    from ultron.core.models import CandidateUrl, ExtractedJob, JobEvaluation

    assert resolve_geo_defaults(None, "confirmed", geo="eu", remote_required=False) == ("unknown", "confirmed")
    assert resolve_geo_defaults(None, "confirmed", geo="eu", remote_required=True) == ("remote_spain", "confirmed")

    job = ExtractedJob(
        title="Product Development Engineer",
        company="IndustrialCo",
        location="Vigo, Pontevedra",
        remote_text="Onsite",
        body_text="Lead industrial product development and polymers scale-up.",
        apply_url="https://jobs.smartrecruiters.com/IndustrialCo/123",
        domain="jobs.smartrecruiters.com",
    )
    ev = JobEvaluation(
        verdict="keep", keep=True, is_senior=True, is_allowed_family=True, is_full_remote=False,
        eligibility="confirmed", eligibility_evidence="Vigo, Pontevedra", open_status_ok=True,
    )
    row = _assemble_final_row(
        job, ev, heur_remote_policy=None, heur_eligibility=None,
        geo_red_flags=False, geo_risk_note="",
        candidate=CandidateUrl(
            query_id="q1", query="", source_url=job.apply_url, normalized_url=job.apply_url, domain=job.domain,
        ),
        profile_criteria={
            "role_focus_terms": ["product development engineer"],
            "remote_policy": {"required": "any"},
            "location_scope": {"preferred": ["Spain", "Galicia"]},
        },
        geo="eu",
    )
    assert row.remote_policy == "unknown"
    assert row.eligibility == "confirmed"


def test_finalize_preserves_agent_high_eligibility_when_heur_remote_policy_none():
    from ultron.core.final_row import _assemble_final_row
    from ultron.core.models import CandidateUrl, ExtractedJob, JobEvaluation

    job = ExtractedJob(
        title="Senior Applied AI Engineer",
        company="JUPUS",
        location="Cologne",
        remote_text="Remote",
        body_text="Fully Remote legal tech role building RAG pipelines.",
        apply_url="https://jobs.ashbyhq.com/jupus/1",
        domain="jobs.ashbyhq.com",
    )
    ev = JobEvaluation(
        verdict="keep", keep=True, is_senior=True, is_allowed_family=True, is_full_remote=True,
        eligibility="high", eligibility_evidence="Germany (EU), Fully Remote", open_status_ok=True,
        evaluation_source="agent",
    )
    row = _assemble_final_row(
        job, ev, heur_remote_policy=None, heur_eligibility=None,
        geo_red_flags=False, geo_risk_note="",
        candidate=CandidateUrl(
            query_id="q1", query="", source_url=job.apply_url, normalized_url=job.apply_url, domain=job.domain,
        ),
        geo="eu",
    )
    assert row.remote_policy == "remote_eu"
    assert row.eligibility == "high"
