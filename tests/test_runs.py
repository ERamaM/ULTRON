import json
import re

import pytest
from pydantic import BaseModel

from ultron import profiles, runs
from ultron.cli import main


@pytest.fixture
def profile_slug():
    profiles.install_example()
    return profiles.EXAMPLE_SLUG


def test_new_run_layout(profile_slug):
    run_id = runs.new_run(profile_slug, "balanced")
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", run_id)
    data = json.loads((runs.run_dir(run_id) / "run.json").read_text(encoding="utf-8"))
    assert data["profile_slug"] == profile_slug
    assert data["preset"] == "balanced"
    assert data["settings"]["max_search_queries"] == 40
    assert data["profile_snapshot"]
    assert data["stages_done"] == []
    assert data["created_at"]
    assert runs.load_run(run_id) == data


def test_require_stage_raises(profile_slug):
    run_id = runs.new_run(profile_slug, "fast")
    with pytest.raises(runs.StageOrderError):
        runs.require_stage(run_id, "fetch")
    runs.mark_stage(run_id, "discover_ats")
    runs.mark_stage(run_id, "discover_ats")
    runs.require_stage(run_id, "fetch")
    assert runs.load_run(run_id)["stages_done"] == ["discover_ats"]
    with pytest.raises(runs.StageOrderError):
        runs.require_stage(run_id, "finalize")


def test_require_stage_no_dependency_is_noop(profile_slug):
    run_id = runs.new_run(profile_slug, "fast")
    runs.require_stage(run_id, "candidates")
    runs.require_stage(run_id, "discover_ats")
    runs.require_stage(run_id, "evaluate")
    with pytest.raises(runs.StageOrderError):
        runs.require_stage(run_id, "fetch")
    runs.mark_stage(run_id, "discover_ats")
    runs.require_stage(run_id, "fetch")


def test_fetch_requires_either_discovery_stage(profile_slug):
    for done in ("discover_ats", "discover_feeds"):
        run_id = runs.new_run(profile_slug, "fast")
        with pytest.raises(runs.StageOrderError) as info:
            runs.require_stage(run_id, "fetch")
        assert info.value.missing == "discover_ats or discover_feeds"
        runs.mark_stage(run_id, done)
        runs.require_stage(run_id, "fetch")


def test_jsonl_roundtrip_utf8(tmp_path):
    class Row(BaseModel):
        title: str

    path = tmp_path / "rows.jsonl"
    title = "Ingeniera de Datos — Señor 🚀"
    assert runs.write_jsonl(path, [Row(title=title), {"title": "b"}]) == 2
    assert runs.append_jsonl(path, [{"title": "c"}]) == 1
    rows = runs.read_jsonl(path)
    assert [r["title"] for r in rows] == [title, "b", "c"]
    assert title in path.read_text(encoding="utf-8")
    assert runs.read_jsonl(tmp_path / "missing.jsonl") == []


@pytest.mark.parametrize("sep", [" ", " ", "\x85", "\x0b", "\x0c", "\x1c", "\x1d", "\x1e"])
def test_jsonl_keeps_unicode_line_separators_inside_values(tmp_path, sep):
    # json.dumps(ensure_ascii=False) leaves these raw; str.splitlines() would cut the record in two.
    path = tmp_path / "rows.jsonl"
    body = f"Remote role{sep}Apply today"
    runs.write_jsonl(path, [{"body": body}, {"body": "b"}])
    runs.append_jsonl(path, [{"body": "c\r\nd"}])
    assert [r["body"] for r in runs.read_jsonl(path)] == [body, "b", "c\r\nd"]


def test_jsonl_tolerates_crlf_files(tmp_path):
    path = tmp_path / "agent_rows.jsonl"
    path.write_bytes(b'{"a": 1}\r\n\r\n{"a": 2}\r\n')
    assert runs.read_jsonl(path) == [{"a": 1}, {"a": 2}]


def test_run_new_cli(profile_slug, capsys):
    assert main(["run", "new", "--profile", profile_slug, "--preset", "fast"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["preset"] == "fast" and out["profile"] == profile_slug
    assert 0 < len(out["query_plan"]) <= 20
    assert len(set(out["query_plan"])) == len(out["query_plan"])
    assert main(["runs", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["runs"][0]["run_id"] == out["run_id"]
    assert main(["runs", "show", out["run_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["run"]["run_id"] == out["run_id"]
    assert main(["runs", "show", "nope"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "run_not_found"
    assert main(["run", "new", "--profile", "nope"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "profile_not_found"
    assert main(["run", "new", "--profile", profile_slug, "--preset", "zzz"]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "unknown_preset"


@pytest.mark.parametrize("bad", ["../x", "..", "nope", "20261116-120000-abcdef/..", "20261116-120000-abcdeg",
                                 "20261116-120000-abcdef\n", "２0261116-120000-abcdef"])
def test_invalid_run_ids_are_not_found(bad):
    with pytest.raises(runs.RunNotFoundError):
        runs.run_dir(bad)
    with pytest.raises(runs.RunNotFoundError):
        runs.load_run(bad)


@pytest.mark.parametrize("argv", [
    ["runs", "show", "../x"], ["seeds", "verify", "--run", "../x", "--from", "f.jsonl"],
    ["discover", "ats", "--run", "../x"], ["discover", "feeds", "--run", "../x"],
    ["candidates", "add", "--run", "../x", "--from", "f.jsonl"], ["fetch", "--run", "../x"],
    ["filter", "--run", "../x"], ["evaluate", "pending", "--run", "../x"],
    ["evaluate", "record", "--run", "../x", "--from", "f.jsonl"], ["finalize", "--run", "../x"],
    ["jobs", "new-since", "../x"], ["export", "xlsx", "--run", "../x"], ["data", "delete-run", "../x"],
    ["diagnose", "https://boards.greenhouse.io/acme/jobs/1", "--profile", profiles.EXAMPLE_SLUG, "--run", "../x"],
])
def test_cli_rejects_traversal_run_ids_with_a_specific_error(argv, capsys):
    assert main(argv) != 0
    out = json.loads(capsys.readouterr().out)
    assert out["error"] != "internal_error", out


def test_build_query_plan_interleaves_site_scopes():
    from ultron.runs_cmd import build_query_plan

    profile = {
        "query_templates": {
            "core_titles": ["Senior ML Engineer", "Staff AI Engineer"],
            "regions": ["Remote EU", "Remote Spain"],
            "site_scopes": ["boards.greenhouse.io", "site:jobs.lever.co", "jobs.ashbyhq.com"],
        }
    }
    plan = build_query_plan(profile, 6)
    assert len(plan) == 6
    assert len(set(plan)) == 6
    assert any(not q.startswith("site:") for q in plan)
    scoped = [q for q in plan if q.startswith("site:")]
    assert len(scoped) == 3
    assert scoped[0].startswith("site:boards.greenhouse.io ")
    assert scoped[1].startswith("site:jobs.lever.co ")
    assert scoped[2].startswith("site:jobs.ashbyhq.com ")
    assert build_query_plan(profile, 0) == []

    no_scopes = {"query_templates": {"core_titles": ["Senior ML Engineer"], "regions": ["Remote EU"]}}
    assert build_query_plan(no_scopes, 10) == ["Senior ML Engineer Remote EU"]
