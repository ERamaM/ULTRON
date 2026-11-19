"""``tools/replay_run.py`` re-runs filter + finalize over a past run without touching it."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
import replay_run
import yaml

from ultron import profiles, runs
from ultron.cli import main

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS = json.loads((FIXTURES / "heuristic_characterization_corpus.json").read_text(encoding="utf-8"))
PROFILE = yaml.safe_load((FIXTURES / "profiles" / "ai-ml-remote-eu.yaml").read_text(encoding="utf-8"))
CASES = {case["id"]: case for case in CORPUS["cases"]}
IDS = ["en-positive-shape-01", "en-positive-shape-03", "en-ambiguous-shape-01", "en-negative-shape-01"]


def _finished_run(capsys, *, evaluate: bool = False, urls: dict[str, str] | None = None) -> str:
    profiles.install_example()
    run_id = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    run = runs.load_run(run_id)
    run["profile_snapshot"] = PROFILE
    runs._write_run(run_id, run)
    rows = []
    for cid in IDS:
        job = {**CORPUS["defaults"], **CASES[cid]["job"]}
        job["apply_url"] = (urls or {}).get(cid, f"https://boards.greenhouse.io/example/jobs/{cid}")
        candidate = {"query_id": "q1", "query": "synthetic", "source_url": job["apply_url"],
                     "normalized_url": job["apply_url"], "domain": "boards.greenhouse.io",
                     "source_type": "ats_api", "source_key": f"greenhouse:{cid}"}
        rows.append({"candidate": candidate, "job": job, "identity_key": f"id:{cid}"})
    runs.write_jsonl(runs.run_dir(run_id) / "postings.jsonl", rows)
    runs.mark_stage(run_id, "fetch")
    assert main(["filter", "--run", run_id]) == 0
    if evaluate:
        ambiguous = {a["identity_key"] for a in runs.read_jsonl(runs.run_dir(run_id) / "ambiguous.jsonl")}
        assert "id:en-ambiguous-shape-01" in ambiguous
        verdict = {"keep": False, "verdict": "discard", "discard_reason": "agent_rejected", "is_senior": True,
                   "is_allowed_family": True, "is_full_remote": True}
        runs.write_jsonl(runs.run_dir(run_id) / "evaluations.jsonl",
                         [{"identity_key": "id:en-ambiguous-shape-01", "evaluation": verdict}])
    assert main(["finalize", "--run", run_id]) == 0
    capsys.readouterr()
    return run_id


def _digest(folder: Path) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir()) if p.is_file()}


def test_replay_matches_the_recorded_run_and_writes_nothing(capsys):
    run_id = _finished_run(capsys)
    folder = runs.run_dir(run_id)
    home = Path(os.environ["ULTRON_HOME"])
    before, home_before = _digest(folder), sorted(p.name for p in home.rglob("*"))
    report = replay_run.replay_run(replay_run.resolve_run_dir(run_id, None))
    assert report["changed_vs_recorded"] == []
    assert report["postings"] == len(IDS)
    statuses = {key: value["replayed"] for key, value in report["identities"].items()}
    assert statuses["id:en-negative-shape-01"] == "discard"
    assert "keep" in statuses.values()
    assert _digest(folder) == before
    assert sorted(p.name for p in home.rglob("*")) == home_before
    assert os.environ["ULTRON_HOME"] == str(home)


def test_recorded_agent_evaluations_are_reused_unless_disabled(capsys):
    run_id = _finished_run(capsys, evaluate=True)
    folder = runs.run_dir(run_id)
    with_agent = replay_run.replay_run(folder)
    assert with_agent["used_evaluations"] is True
    assert with_agent["changed_vs_recorded"] == []
    assert with_agent["identities"]["id:en-ambiguous-shape-01"]["replayed"] == "discard"
    without = replay_run.replay_run(folder, use_evaluations=False)
    assert without["identities"]["id:en-ambiguous-shape-01"]["replayed"] == "unsure"
    assert without["changed_vs_recorded"] == ["id:en-ambiguous-shape-01"]
    diff = replay_run.diff_reports(with_agent, without)
    assert diff["same_run"] is True
    assert diff["transitions"] == {"discard->unsure": 1}
    assert diff["changes"][0]["identity_key"] == "id:en-ambiguous-shape-01"


def test_redacted_export_urls_still_count_as_ranked(capsys):
    fake_token = "abcdef" + "123456"  # synthetic; built at runtime so secret scanners don't flag the literal
    url = f"https://boards.greenhouse.io/example/jobs/1?gh_jid=1&access_token={fake_token}"
    run_id = _finished_run(capsys, urls={"en-positive-shape-01": url})
    exported = json.loads((runs.run_dir(run_id) / "results.json").read_text(encoding="utf-8"))["rows"]
    assert url not in {row["apply_url"] for row in exported}
    report = replay_run.replay_run(runs.run_dir(run_id))
    entry = report["identities"]["id:en-positive-shape-01"]
    assert entry["recorded"] == "ranked"
    assert entry["replayed"] == "keep" and entry["rank"] is not None
    assert report["changed_vs_recorded"] == []


def test_postings_sharing_one_url_are_mapped_once(capsys):
    shared = "https://boards.greenhouse.io/example/jobs/shared"
    run_id = _finished_run(capsys, urls={"en-positive-shape-01": shared, "en-positive-shape-03": shared})
    report = replay_run.replay_run(runs.run_dir(run_id))
    assert report["changed_vs_recorded"] == []
    pair = [report["identities"][f"id:{cid}"] for cid in ("en-positive-shape-01", "en-positive-shape-03")]
    ranks = [entry["rank"] for entry in pair if entry["replayed"] != "discard"]
    assert ranks and None not in ranks and len(set(ranks)) == len(ranks)


def test_a_run_that_stopped_after_filter_is_pending(capsys):
    profiles.install_example()
    run_id = _finished_run(capsys)
    (runs.run_dir(run_id) / "results.json").unlink()
    report = replay_run.replay_run(runs.run_dir(run_id))
    assert report["changed_vs_recorded"] == []
    assert {v["recorded"] for k, v in report["identities"].items() if k != "id:en-negative-shape-01"} == {"pending"}


def test_cli_replay_and_diff(tmp_path, capsys):
    run_id = _finished_run(capsys)
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    assert replay_run.main(["replay", "--run", run_id, "--output", str(first)]) == 0
    assert replay_run.main(["replay", "--run-dir", str(runs.run_dir(run_id)), "--output", str(second)]) == 0
    assert replay_run.main(["diff", str(first), str(second)]) == 0
    assert json.loads(capsys.readouterr().out)["changed"] == 0


@pytest.mark.parametrize("args", [["replay", "--run", "not-a-run"], ["replay", "--run", "20990101-000000-abcdef"]])
def test_cli_rejects_unknown_runs(args, capsys):
    assert replay_run.main(args) == 2
    assert "error" in json.loads(capsys.readouterr().out)
    assert not (Path(os.environ["ULTRON_HOME"]) / "runs" / "20990101-000000-abcdef").exists()


def test_diff_rejects_foreign_reports(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema_version": 7}), encoding="utf-8")
    assert replay_run.main(["diff", str(bad), str(bad)]) == 2
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")
    assert replay_run.main(["diff", str(tmp_path / "broken.json"), str(bad)]) == 2
    capsys.readouterr()
