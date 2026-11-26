"""ULTRON-versus-baseline arm comparison: session files, ingestion, scoring and the CLI."""

from __future__ import annotations

import json
import os

import arm_comparison as ac
import arm_inputs as ai
import arm_report
import pytest
import safe_paths
import yaml

PROFILE = {"slug": "synthetic", "name": "Synthetic ML", "source_cv": "/home/someone/cv.pdf", "enabled": True,
           "criteria": {"target_titles": ["ML Engineer"], "vocabulary_packs": ["ai-ml"]}}
RUN_IDS = ["20261125-100000-aaaaaa", "20261125-110000-bbbbbb", "20261125-120000-cccccc"]


def _row(n, title=None):
    return {"title": title or f"ML Engineer {n}", "company": "Acme", "apply_url": f"https://jobs.example.test/{n}",
            "location": "Remote EU"}


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setattr(ac, "load_profile", lambda slug: dict(PROFILE))
    ac.prepare("synthetic", "s1", top_n=3)
    return "s1"


@pytest.fixture
def home(tmp_path, monkeypatch):
    folder = tmp_path / "ultron-home"
    (folder / "runs").mkdir(parents=True)
    monkeypatch.setenv("ULTRON_HOME", str(folder))
    return folder


def _make_run(home, run_id, rows, *, profile=None, stages=("finalize",), slug="synthetic", results=True,
              preset="balanced", mode=None, extra=None):
    folder = home / "runs" / run_id
    folder.mkdir()
    stages = list(stages) if isinstance(stages, tuple) else stages
    run = {"run_id": run_id, "profile_slug": slug, "preset": preset, "stages_done": stages,
           "profile_snapshot": profile or dict(PROFILE)}
    if mode:
        run["mode"] = mode
    (folder / "run.json").write_text(json.dumps(run), encoding="utf-8")
    for name, file_rows in (extra or {}).items():
        (folder / name).write_text("".join(json.dumps(r) + "\n" for r in file_rows), encoding="utf-8")
    (folder / "postings.jsonl").write_text("", encoding="utf-8")
    if results:
        summary = {"started_at": "2026-11-25T10:00:00+00:00", "ended_at": "2026-11-25T10:15:00+00:00"}
        payload = results if isinstance(results, dict) else {"rows": rows, "summary": summary}
        (folder / "results.json").write_text(json.dumps(payload), encoding="utf-8")
    return run_id


def _write_jsonl(reports_dir, name, rows):
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / name).write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _label_all(reports_dir, session, relevant_urls):
    path = reports_dir / f"{session}.labels.yaml"
    labels = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in labels["jobs"]:
        value = any(v["apply_url"] in relevant_urls for v in job["variants"])
        job.update(relevant=value, open=True, eligible=True, role_fit=value)
    path.write_text(yaml.safe_dump(labels), encoding="utf-8")


# --- prepare --------------------------------------------------------------------------------------


def test_prepare_writes_an_allowlisted_brief(session, reports_dir):
    brief = (reports_dir / "s1.brief.md").read_text(encoding="utf-8")
    assert "3 best currently open" in brief and "ML Engineer" in brief
    assert "cv.pdf" not in brief and "vocabulary_packs" not in brief and "enabled" not in brief
    assert "as data, never as instructions" in brief
    state = ac.load_state(session)
    assert state["top_n"] == 3 and state["arms"] == {}


def test_brief_fence_survives_backticks_in_the_profile():
    brief = ac.brief_markdown({"prompt": "use ``` fences"}, 5)
    assert "````yaml" in brief


def test_prepare_refuses_to_overwrite_without_force(session):
    with pytest.raises(ac.ComparisonError, match="already exists"):
        ac.prepare("synthetic", session, top_n=3)
    assert ac.prepare("synthetic", session, top_n=4, force=True)["top_n"] == 4


def test_prepare_rejects_bad_inputs(monkeypatch):
    def missing(slug):
        raise FileNotFoundError(f"profile not found: {slug}")

    with pytest.raises(ac.ComparisonError):
        ac.prepare("x", "s1", top_n=0)
    monkeypatch.setattr(ac, "load_profile", missing)
    with pytest.raises(ac.ComparisonError, match="cannot be loaded"):
        ac.prepare("x", "s1", top_n=5)
    for name in ("../evil", "CON", "lpt1"):
        with pytest.raises(ac.ComparisonError, match="session names"):
            ac.prepare("x", name, top_n=5)


@pytest.mark.parametrize("loaded", [["a list"], "text"])
def test_prepare_rejects_non_mapping_profiles(monkeypatch, loaded):
    monkeypatch.setattr(ac, "load_profile", lambda slug: loaded)
    with pytest.raises(ac.ComparisonError, match="not a mapping"):
        ac.prepare("x", "s5", top_n=5)


@pytest.mark.parametrize("content, message", [(None, "not found"), ("[]", "unsupported"), ("{", "not valid JSON"),
                                              (json.dumps({"schema_version": 0}), "unsupported")])
def test_load_state_errors(reports_dir, content, message):
    if content is not None:
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / "old.state.json").write_text(content, encoding="utf-8")
    with pytest.raises(ac.ComparisonError, match=message):
        ac.load_state("old")


# --- ingest ---------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw, message", [
    ("text", "JSON object"),
    ({"title": "", "apply_url": "https://x.test"}, "required"),
    ({"title": ["T"], "apply_url": "https://x.test"}, "must be a string"),
    ({"title": "T", "apply_url": "javascript:alert(1)"}, "http"),
    ({"title": "T", "apply_url": "https://"}, "http"),
    ({"title": "T", "apply_url": "https://[bad/"}, "malformed"),
    ({"title": "T", "apply_url": "https://x.test/" + "a" * 2100}, "longer"),
    ({"title": "T", "apply_url": "https://x.test/job?access_token=abcdef1234567890abcdef"}, "credential"),
    ({"title": "T", "apply_url": "https://user:pw@x.test/job"}, "user name or password"),
])
def test_clean_row_rejects_unusable_rows(raw, message):
    with pytest.raises(ac.ComparisonError, match=message):
        ai.clean_row(raw, 1)


def test_clean_row_keeps_long_urls_whole():
    url = "https://jobs.example.test/" + "a" * 600 + "?req=42"
    assert ai.clean_row({"title": "T", "apply_url": url}, 1)["apply_url"] == url


def test_ingest_agent_and_validation(session, reports_dir):
    _write_jsonl(reports_dir, "b1.jsonl", [_row(1), _row(2)])
    assert ac.ingest_agent(session, 1, "b1.jsonl")["rows"] == 2
    (reports_dir / "bad.jsonl").write_text("{not json}\n", encoding="utf-8")
    with pytest.raises(ac.ComparisonError, match="line 1"):
        ac.ingest_agent(session, 1, "bad.jsonl")
    with pytest.raises(ac.ComparisonError, match="rep"):
        ac.ingest_agent(session, 0, "b1.jsonl")
    with pytest.raises(ac.ComparisonError, match="arm names"):
        ac.ingest_agent(session, 1, "b1.jsonl", arm="Bad Arm")
    with pytest.raises(ac.ComparisonError, match="ingest-ultron"):
        ac.ingest_agent(session, 1, "b1.jsonl", arm="ultron")
    with pytest.raises(ac.ComparisonError, match="not one of"):
        ac.ingest_agent(session, 1, "b1.jsonl", arm="other")
    with pytest.raises(ac.ComparisonError, match="plain file name"):
        ac.ingest_agent(session, 1, "../b1.jsonl")


def test_ingest_agent_refuses_the_same_file_twice(session, reports_dir):
    _write_jsonl(reports_dir, "b1.jsonl", [_row(1)])
    ac.ingest_agent(session, 1, "b1.jsonl")
    with pytest.raises(ac.ComparisonError, match="already repetition 1"):
        ac.ingest_agent(session, 2, "b1.jsonl")


def test_only_top_n_rows_are_validated_and_kept(session, reports_dir):
    _write_jsonl(reports_dir, "b1.jsonl", [_row(1), _row(2), _row(3), {"title": "bad"}])
    assert ac.ingest_agent(session, 1, "b1.jsonl") == {"session": session, "arm": "baseline", "rep": 1,
                                                       "rows": 3, "submitted": 4}


def test_parse_jsonl_keeps_unicode_line_separators():
    assert ai.parse_jsonl('{"title": "a b"}\n') == [{"title": "a b"}]


@pytest.mark.skipif(not hasattr(os, "symlink") or os.name == "nt", reason="symlinks need privileges on Windows")
def test_ingest_agent_rejects_symlinks(session, reports_dir, tmp_path):
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps(_row(1)) + "\n", encoding="utf-8")
    os.symlink(outside, reports_dir / "link.jsonl")
    with pytest.raises(ac.ComparisonError, match="link"):
        ac.ingest_agent(session, 1, "link.jsonl")


def test_ingest_ultron_reads_results_and_pipeline_time(session, home):
    run_id = _make_run(home, RUN_IDS[0], [_row(1)])
    assert ac.ingest_ultron(session, 1, run_id)["rows"] == 1
    entry = ac.load_state(session)["arms"]["ultron"]["1"]
    assert entry["pipeline_seconds"] == 900.0 and entry["source"] == run_id and entry["cost"] == {}


@pytest.mark.parametrize("kwargs, message", [
    ({"slug": "other"}, "used profile"),
    ({"profile": {**PROFILE, "criteria": {"target_titles": ["Nurse"]}}}, "different version"),
    ({"profile": {**PROFILE, "source_cv": "/home/someone/cv-v2.pdf"}}, "different version"),
    ({"preset": "fast"}, "preset"),
    ({"stages": ()}, "not finalized"),
    ({"stages": "finalize"}, "not finalized"),
    ({"results": False}, "no results.json"),
    ({"results": {"rows": "x"}}, "rows list"),
])
def test_ingest_ultron_validates_the_run(session, home, kwargs, message):
    run_id = _make_run(home, RUN_IDS[0], [_row(1)], **kwargs)
    with pytest.raises(ac.ComparisonError, match=message):
        ac.ingest_ultron(session, 1, run_id)


def test_ingest_ultron_refuses_the_same_run_twice(session, home):
    run_id = _make_run(home, RUN_IDS[0], [_row(1)])
    ac.ingest_ultron(session, 1, run_id)
    assert ac.ingest_ultron(session, 1, run_id)["rows"] == 1  # re-ingesting the same rep is fine
    with pytest.raises(ac.ComparisonError, match="already repetition 1"):
        ac.ingest_ultron(session, 2, run_id)


def test_ingest_ultron_unknown_run(session, home):
    with pytest.raises(ac.ComparisonError, match="run"):
        ac.ingest_ultron(session, 1, "bad-id")


@pytest.mark.parametrize("text, message", [("[]", "not a JSON object"), ("{", "not valid JSON")])
def test_json_object_errors(text, message):
    with pytest.raises(ai.ComparisonError, match=message):
        ai.json_object(text, "results.json")


def test_unreadable_report_is_a_clean_error(session, reports_dir):
    (reports_dir / "bin.jsonl").write_bytes(bytes([0xFF, 0xFE, 0x00]))
    with pytest.raises(ac.ComparisonError, match="unreadable"):
        ac.ingest_agent(session, 1, "bin.jsonl")


def test_run_seconds_edge_cases():
    assert ai.run_seconds({}) is None
    assert ai.run_seconds({"started_at": "2026-11-25T10:00:00", "ended_at": "2026-11-25T09:00:00"}) is None
    assert ai.run_seconds({"started_at": "2026-11-25T10:00:00", "ended_at": "2026-11-25T11:00:00+00:00"}) is None


def test_record_cost(session, reports_dir):
    with pytest.raises(ac.ComparisonError, match="not ingested"):
        ac.record_cost(session, "baseline", 1, tokens=5)
    _write_jsonl(reports_dir, "b1.jsonl", [_row(1)])
    ac.ingest_agent(session, 1, "b1.jsonl")
    ac.record_cost(session, "baseline", 1, tokens=100, tool_calls=None, seconds=5.5)
    ac.record_cost(session, "baseline", 1, tool_calls=7)
    assert ac.load_state(session)["arms"]["baseline"]["1"]["cost"] == {"tokens": 100, "seconds": 5.5, "tool_calls": 7}
    ac.ingest_agent(session, 1, "b1.jsonl")
    assert ac.load_state(session)["arms"]["baseline"]["1"]["cost"]["tokens"] == 100  # same source keeps cost


@pytest.mark.parametrize("cost, message", [
    ({}, "at least one"), ({"tokens": -1}, "finite"), ({"seconds": float("inf")}, "finite"),
    ({"tokens": float("nan")}, "finite"), ({"tool_calls": 2.5}, "whole"),
])
def test_record_cost_rejects_bad_values(session, cost, message):
    with pytest.raises(ac.ComparisonError, match=message):
        ac.record_cost(session, "baseline", 1, **cost)


# --- pool, spot check, score ----------------------------------------------------------------------


def _ingest_rep(session, home, reports_dir, rep):
    _make_run(home, RUN_IDS[rep - 1], [_row(1), _row(2), _row(3), _row(4)])
    ac.ingest_ultron(session, rep, RUN_IDS[rep - 1])
    _write_jsonl(reports_dir, f"b{rep}.jsonl", [_row(3), _row(5)])
    ac.ingest_agent(session, rep, f"b{rep}.jsonl")
    for arm in ("ultron", "baseline"):
        ac.record_cost(session, arm, rep, tokens=2000, tool_calls=10, seconds=100)


def _full_experiment(session, home, reports_dir, reps=3):
    for rep in range(1, reps + 1):
        _ingest_rep(session, home, reports_dir, rep)
    ac.pool(session)


def _fill_spotcheck(reports_dir, session, size=2):
    ac.spotcheck(session, size)
    path = reports_dir / f"{session}.spotcheck.yaml"
    spot = yaml.safe_load(path.read_text(encoding="utf-8"))
    labels = {job["id"]: job["relevant"] for job in
              yaml.safe_load((reports_dir / f"{session}.labels.yaml").read_text(encoding="utf-8"))["jobs"]}
    for job in spot["jobs"]:
        job["relevant"] = labels[job["id"]]
    path.write_text(yaml.safe_dump(spot), encoding="utf-8")
    return ac.spotcheck(session, size)


def test_pool_only_lists_scored_rows_and_hides_arms(session, home, reports_dir):
    _full_experiment(session, home, reports_dir, reps=1)
    text = (reports_dir / "s1.labels.yaml").read_text(encoding="utf-8")
    assert "jobs.example.test/4" not in text  # rank 4 is below top N = 3
    assert "ultron" not in text and "baseline" not in text
    assert ac.pool(session)["pending"] == 4


def test_pool_resets_labels_of_merged_jobs(session, home, reports_dir):
    _full_experiment(session, home, reports_dir, reps=1)
    _label_all(reports_dir, session, {"https://jobs.example.test/1"})
    bridge = {**_row(2), "apply_url": "https://jobs.example.test/5"}  # joins job 2 and job 5
    _write_jsonl(reports_dir, "b2.jsonl", [bridge])
    ac.ingest_agent(session, 2, "b2.jsonl")
    with pytest.raises(ac.ComparisonError, match="merged or split"):
        ac.build_report(session, allow_partial=True)
    result = ac.pool(session)
    assert len(result["relabel_ids"]) == 1 and result["pending"] == 1


def test_score_end_to_end(session, home, reports_dir):
    _full_experiment(session, home, reports_dir)
    _label_all(reports_dir, session, {"https://jobs.example.test/1", "https://jobs.example.test/3"})
    assert _fill_spotcheck(reports_dir, session)["relabel"] is False
    report = ac.build_report(session)
    assert report["complete"] is True and report["spotcheck"]["compared"] == 2
    ultron = report["arms"]["ultron"]
    assert ultron["repetitions"]["1"]["rows"] == 3
    assert ultron["repetitions"]["1"]["precision"] == pytest.approx(0.6667)
    assert ultron["only_this_arm_relevant"] == 1 and ultron["reproducibility_jaccard"] == 1.0
    assert report["arms"]["baseline"]["cost_per_relevant"]["tokens"] == 2000
    assert report["pipeline_seconds"] == {"ultron": {"1": 900.0, "2": 900.0, "3": 900.0}}
    assert ac.score(session, "report.json")["complete"] is True
    markdown = (reports_dir / "report.md").read_text(encoding="utf-8")
    assert "| Precision |" in markdown and "Partial" not in markdown and "(n=3)" in markdown


def test_score_refuses_incomplete_experiments(session, home, reports_dir):
    with pytest.raises(ac.ComparisonError, match="no repetitions"):
        ac.build_report(session)
    _full_experiment(session, home, reports_dir, reps=1)
    with pytest.raises(ac.ComparisonError, match="at least 3"):
        ac.build_report(session)
    _write_jsonl(reports_dir, "b9.jsonl", [_row(7)])
    ac.ingest_agent(session, 9, "b9.jsonl")
    with pytest.raises(ac.ComparisonError, match="run pool again"):
        ac.build_report(session, allow_partial=True)
    ac.pool(session)
    report = ac.build_report(session, allow_partial=True)
    problems = " ".join(report["problems"])
    assert "different repetitions" in problems and "no tokens recorded" in problems and "spot check" in problems
    assert report["complete"] is False and report["pending_jobs"] == 5
    assert "Partial result" in arm_report.report_markdown(report)


def test_score_refuses_pending_labels(session, home, reports_dir):
    _full_experiment(session, home, reports_dir)
    with pytest.raises(ac.ComparisonError, match="spot check"):
        ac.build_report(session)
    _label_all(reports_dir, session, set())
    _fill_spotcheck(reports_dir, session)
    labels = (reports_dir / "s1.labels.yaml").read_text(encoding="utf-8")
    (reports_dir / "s1.labels.yaml").write_text(labels.replace("relevant: false", "relevant: pending", 1),
                                                encoding="utf-8")
    with pytest.raises(ac.ComparisonError, match="pending labels"):
        ac.build_report(session)


@pytest.mark.parametrize("spot, message", [
    (None, "no spot check"),
    ({"stale": ["j1"], "pending": 0, "compared": 2, "relabel": False}, "no longer in the pool"),
    ({"stale": [], "pending": 2, "compared": 2, "relabel": False}, "2 pending"),
    ({"stale": [], "pending": 0, "compared": 0, "relabel": False}, "no true/false"),
    ({"stale": [], "pending": 0, "compared": 2, "relabel": True}, "relabel"),
    ({"stale": [], "pending": 0, "compared": 2, "relabel": False}, None),
])
def test_spotcheck_problems(spot, message):
    problems = ac._spotcheck_problems(spot)
    assert (message in problems[0]) if message else problems == []


@pytest.mark.parametrize("output, message", [
    ("report.txt", r"\.json"), ("s1.state.json", "reserved"), ("s1.Brief.json", "reserved"), ("../r.json", "plain"),
    ("nul.json", "reserved"),
])
def test_score_output_names(session, output, message):
    with pytest.raises((ac.ComparisonError, safe_paths.UnsafePathError), match=message):
        ac.score(session, output)


def test_spotcheck_flow(session, home, reports_dir):
    _full_experiment(session, home, reports_dir, reps=1)
    with pytest.raises(ac.ComparisonError, match="size"):
        ac.spotcheck(session, 0)
    assert ac.spotcheck(session, 2)["jobs"] == 2
    path = reports_dir / "s1.spotcheck.yaml"
    spot = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in spot["jobs"]:
        job["relevant"] = True
    path.write_text(yaml.safe_dump(spot), encoding="utf-8")
    assert ac.spotcheck(session, 2)["pending"] == 2  # the main labels are still pending
    _label_all(reports_dir, session, {"https://jobs.example.test/1"})
    result = ac.spotcheck(session, 2)
    assert result["compared"] == 2 and result["pending"] == 0
    assert result["size"] == 2 and result["stale"] == []
    path.write_text("session: s1\njobs: 3\n", encoding="utf-8")
    with pytest.raises(ac.ComparisonError, match="jobs"):
        ac.spotcheck(session, 2)


def test_spotcheck_is_recomputed_after_relabelling(session, home, reports_dir):
    _full_experiment(session, home, reports_dir)
    _label_all(reports_dir, session, {"https://jobs.example.test/1", "https://jobs.example.test/3"})
    _fill_spotcheck(reports_dir, session, size=4)
    assert ac.build_report(session)["complete"] is True
    _label_all(reports_dir, session, set())  # the main labels now disagree with the user's sample
    with pytest.raises(ac.ComparisonError, match="disagreement"):
        ac.build_report(session)


def test_spotcheck_goes_stale_when_the_pool_changes(session, home, reports_dir):
    _full_experiment(session, home, reports_dir)
    _label_all(reports_dir, session, {"https://jobs.example.test/1"})
    _fill_spotcheck(reports_dir, session, size=4)
    ac.discard(session, "baseline", 3)
    ac.discard(session, "baseline", 2)
    ac.discard(session, "baseline", 1)
    ac.pool(session)
    report = ac.build_report(session, allow_partial=True)
    assert report["spotcheck"]["stale"] and any("no longer in the pool" in p for p in report["problems"])


def test_discard_drops_a_repetition(session, reports_dir):
    _write_jsonl(reports_dir, "b1.jsonl", [_row(1)])
    ac.ingest_agent(session, 1, "b1.jsonl")
    result = ac.discard(session, "baseline", 1)
    assert result["discarded"] == {"arm": "baseline", "rep": 1}
    assert "baseline" not in ac.load_state(session)["arms"]
    with pytest.raises(ac.ComparisonError, match="not ingested"):
        ac.discard(session, "baseline", 1)


def test_force_removes_stale_labels(session, home, reports_dir):
    _full_experiment(session, home, reports_dir, reps=1)
    ac.spotcheck(session, 1)
    ac.prepare("synthetic", session, top_n=3, force=True)
    assert not (reports_dir / "s1.labels.yaml").exists() and not (reports_dir / "s1.spotcheck.yaml").exists()


def test_bad_labels_file_is_reported(session, reports_dir):
    (reports_dir / "s1.labels.yaml").write_text("session: s1\njobs:\n- {id: j1, relevant: 1}\n", encoding="utf-8")
    with pytest.raises(ac.ComparisonError, match="s1.labels.yaml"):
        ac.load_labels(session)


def test_report_markdown_formats_missing_values():
    stat = {"mean": None, "std": None, "n": 0}
    arm = {"aggregate": {key: stat for _, key in arm_report.REPORT_ROWS},
           "cost_per_relevant": {"tokens": None, "tool_calls": None, "seconds": 2.5},
           **{key: None for _, key in arm_report.ARM_ROWS}}
    report = {"session": "s", "complete": True, "top_n": 1, "pool": {"jobs": 0, "relevant": 0},
              "ultron_version": "0", "pipeline_seconds": {}, "spotcheck": None, "arms": {"a": arm},
              "between_arms": {"a|b": {"jaccard_all": None, "jaccard_relevant": 0.5}}}
    text = arm_report.report_markdown(report)
    assert "None" not in text and "2.5" in text and "relevant 0.5" in text


# --- CLI ------------------------------------------------------------------------------------------


def test_cli_end_to_end(session, home, reports_dir, capsys):
    assert ac.main(["score", "--session", session]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False
    _make_run(home, RUN_IDS[0], [_row(1)])
    assert ac.main(["ingest-ultron", "--session", session, "--rep", "1", "--run", RUN_IDS[0]]) == 0
    _write_jsonl(reports_dir, "b1.jsonl", [_row(1)])
    assert ac.main(["ingest-agent", "--session", session, "--rep", "1", "--from", "b1.jsonl"]) == 0
    assert ac.main(["record-cost", "--session", session, "--arm", "baseline", "--rep", "1", "--tokens", "5"]) == 0
    assert ac.main(["pool", "--session", session]) == 0
    assert ac.main(["spotcheck", "--session", session]) == 0
    capsys.readouterr()
    assert ac.main(["score", "--session", session, "--allow-partial"]) == 0
    assert json.loads(capsys.readouterr().out)["top_n"] == 3
    assert ac.main(["discard", "--session", session, "--arm", "baseline", "--rep", "1"]) == 0


def test_cli_reports_file_system_errors(session, monkeypatch, capsys):
    def broken(*_args, **_kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(ac, "load_state", broken)
    assert ac.main(["pool", "--session", session]) == 2
    assert "file system error" in capsys.readouterr().out


def test_cli_prepare(monkeypatch, capsys):
    monkeypatch.setattr(ac, "load_profile", lambda slug: dict(PROFILE))
    assert ac.main(["prepare", "--profile", "synthetic", "--session", "s2"]) == 0
    assert json.loads(capsys.readouterr().out)["top_n"] == ac.DEFAULT_TOP_N


# --- several arms, second phase, trace ------------------------------------------------------------


@pytest.fixture
def three_arms(monkeypatch):
    monkeypatch.setattr(ac, "load_profile", lambda slug: dict(PROFILE))
    result = ac.prepare("synthetic", "s3", top_n=3, arms="baseline,ultron,scout=balanced/scout")
    assert set(result["arms"]) == {"baseline", "ultron", "scout"}
    return "s3"


def test_ingest_ultron_checks_the_arm_mode(three_arms, home, reports_dir):
    sweep = _make_run(home, RUN_IDS[0], [_row(1)])
    with pytest.raises(ac.ComparisonError, match="mode 'sweep', not 'scout'"):
        ac.ingest_ultron(three_arms, 1, sweep, arm="scout")
    scout = _make_run(home, RUN_IDS[1], [_row(2)], mode="scout")
    assert ac.ingest_ultron(three_arms, 1, scout, arm="scout")["rows"] == 1
    entry = ac.load_state(three_arms)["arms"]["scout"]["1"]
    assert entry["ultron_home"] == os.path.abspath(str(home))
    with pytest.raises(ac.ComparisonError, match="agent arm"):
        ac.ingest_ultron(three_arms, 1, scout, arm="baseline")
    _write_jsonl(reports_dir, "s.jsonl", [_row(1)])
    with pytest.raises(ac.ComparisonError, match="ULTRON arm"):
        ac.ingest_agent(three_arms, 1, "s.jsonl", arm="scout")


def test_prepare_rejects_bad_arms(monkeypatch):
    monkeypatch.setattr(ac, "load_profile", lambda slug: dict(PROFILE))
    with pytest.raises(ac.ComparisonError, match="at least two"):
        ac.prepare("synthetic", "s4", top_n=3, arms="baseline")


def _three_arm_experiment(session, home, reports_dir):
    for rep in (1, 2, 3):
        ac.ingest_ultron(session, rep, _make_run(home, f"20261125-10000{rep}-aaaaaa", [_row(1), _row(2)]))
        ac.ingest_ultron(session, rep, _make_run(home, f"20261125-11000{rep}-bbbbbb", [_row(1), _row(4)],
                                                 mode="scout"), arm="scout")
        _write_jsonl(reports_dir, f"b{rep}.jsonl", [_row(3), _row(4)])
        ac.ingest_agent(session, rep, f"b{rep}.jsonl")
        for arm in ("baseline", "ultron", "scout"):
            ac.record_cost(session, arm, rep, tokens=1000, tool_calls=10, seconds=100)
    ac.ingest_ultron(session, 1, _make_run(home, "20261126-100000-cccccc", [_row(5), _row(1)], mode="scout"),
                     arm="scout", phase=2)
    ac.record_cost(session, "scout", 1, 2, tokens=200, tool_calls=2, seconds=20)
    _write_jsonl(reports_dir, "b2d.jsonl", [_row(6)])
    ac.ingest_agent(session, 1, "b2d.jsonl", phase=2)
    ac.pool(session)
    _label_all(reports_dir, session, {_row(n)["apply_url"] for n in (1, 3, 4, 5, 6)})
    _fill_spotcheck(reports_dir, session, size=4)


def test_three_arm_report_with_a_second_phase(three_arms, home, reports_dir):
    _three_arm_experiment(three_arms, home, reports_dir)
    report = ac.build_report(three_arms)
    assert report["complete"] is True and set(report["arms"]) == {"baseline", "ultron", "scout"}
    assert set(report["between_arms"]) == {"baseline|scout", "baseline|ultron", "scout|ultron"}
    phase = report["second_phase"]
    assert phase["new_relevant_in_pool"] == 2
    assert phase["arms"]["scout@2"]["new_relevant_found"] == 1
    assert phase["arms"]["scout@2"]["tokens_per_run"] == 200
    assert phase["arms"]["scout@2"]["first_phase_tokens_per_run"] == 1000
    assert set(report["pipeline_seconds"]) == {"ultron", "scout", "scout@2"}
    ac.score(three_arms, "r3.json")
    markdown = (reports_dir / "r3.md").read_text(encoding="utf-8")
    assert "## Second phase" in markdown and "scout@2" in markdown


def test_record_cost_and_discard_by_phase(three_arms, home, reports_dir):
    _write_jsonl(reports_dir, "b1.jsonl", [_row(1)])
    ac.ingest_agent(three_arms, 1, "b1.jsonl", phase=2)
    ac.record_cost(three_arms, "baseline", 1, 2, tokens=5)
    assert ac.load_state(three_arms)["arms"]["baseline@2"]["1"]["cost"] == {"tokens": 5}
    with pytest.raises(ac.ComparisonError, match="not ingested"):
        ac.record_cost(three_arms, "baseline", 1, tokens=5)
    ac.discard(three_arms, "baseline", 1, 2)
    assert "baseline@2" not in ac.load_state(three_arms)["arms"]


def test_trace_explains_missed_jobs(three_arms, home, reports_dir):
    def identity(n):
        from ultron.core.job_identity import canonical_job_identity
        return canonical_job_identity(_row(n)["apply_url"]).identity_key

    discarded = [{"apply_url": _row(3)["apply_url"], "stage": "pre_filter", "reason": "non_eu_remote"}]
    for rep in (1, 2, 3):
        ac.ingest_ultron(three_arms, rep, _make_run(
            home, f"20261125-11000{rep}-bbbbbb", [_row(1)], mode="scout",
            extra={"discarded.jsonl": discarded, "candidates.jsonl": [{"normalized_url": _row(9)["apply_url"]}]},
        ), arm="scout")
    _write_jsonl(reports_dir, "b1.jsonl", [_row(3), _row(4)])
    ac.ingest_agent(three_arms, 1, "b1.jsonl")
    ac.pool(three_arms)
    _label_all(reports_dir, three_arms, {_row(n)["apply_url"] for n in (1, 3, 4)})
    result = ac.trace(three_arms, "scout")
    assert result["relevant_missed"] == 2
    assert result["outcome_counts"] == {"discarded:pre_filter:non_eu_remote": 3, "employer_never_queried": 3}
    assert identity(1)
    written = ac.trace(three_arms, "scout", "t3.json")
    assert written["relevant_missed"] == 2 and (reports_dir / "t3.json").exists()
    with pytest.raises(ac.ComparisonError, match="agent arm"):
        ac.trace(three_arms, "baseline")
    with pytest.raises(ac.ComparisonError, match="no repetitions"):
        ac.trace(three_arms, "ultron")


def test_cli_three_arms(three_arms, home, reports_dir, capsys):
    run_id = _make_run(home, RUN_IDS[0], [_row(1)], mode="scout")
    (home / "runs" / run_id / "postings.jsonl").unlink()  # Scout saves final rows without a sweep postings file.
    args = ["ingest-ultron", "--session", three_arms, "--rep", "1", "--run", run_id, "--arm", "scout", "--phase", "2"]
    assert ac.main(args) == 0
    capsys.readouterr()
    assert ac.main(["trace", "--session", three_arms, "--arm", "scout"]) == 2
    assert "no repetitions" in capsys.readouterr().out
