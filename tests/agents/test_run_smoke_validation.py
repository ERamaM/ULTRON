"""Validation logic of ``tests/agents/run_smoke.py`` against a synthetic ULTRON_HOME.

Real agents never run here: only the part that inspects ``ULTRON_HOME`` after an agent run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent


def _load_run_smoke():
    spec = importlib.util.spec_from_file_location("run_smoke", HERE / "run_smoke.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_smoke"] = module
    spec.loader.exec_module(module)
    return module


rs = _load_run_smoke()

FULL = ["new", "seeds", "discover_feeds", "discover_ats", "candidates", "fetch", "filter", "evaluate", "finalize"]
RUN_A = "20261116-101500-abc123"
RUN_B = "20261116-111500-def456"


def _make_run(home: Path, run_id: str, stages: list[str], *, seeds_rows: int = 2, results: bool = True,
              agent_files: tuple[str, ...] = ("agent_seeds.jsonl",), profile: str = "backend-latam") -> Path:
    rd = home / "runs" / run_id
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({
        "run_id": run_id, "profile_slug": profile, "preset": "fast", "created_at": "2026-11-16T10:15:00+00:00",
        "stages_done": stages,
    }), encoding="utf-8")
    if seeds_rows >= 0:
        (rd / "seeds_verified.jsonl").write_text(
            "".join(json.dumps({"company": f"C{i}", "status": "verified"}) + "\n" for i in range(seeds_rows)),
            encoding="utf-8")
    if results:
        (rd / "results.md").write_text("# Results\n\n| a | b |\n", encoding="utf-8")
    for name in agent_files:
        (rd / name).write_text("{}\n", encoding="utf-8")
    return rd


def _listing(home: Path) -> list[dict]:
    out = []
    for path in sorted((home / "runs").glob("*/run.json"), reverse=True):
        data = json.loads(path.read_text(encoding="utf-8"))
        out.append({k: data.get(k) for k in ("run_id", "profile_slug", "preset", "created_at", "stages_done")})
    return out


def test_complete_run_reported_by_agent_passes(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, FULL)
    result = rs.validate(home, _listing(home), f"Done. run_id: {RUN_A}")
    assert result["ok"] is True, result
    assert result["run_id"] == RUN_A
    assert result["failures"] == []
    ev = result["evidence"]
    assert ev["seeds_verified_rows"] == 2
    assert ev["profile_slug"] == "backend-latam"
    assert ev["agent_files"] == ["agent_seeds.jsonl"]
    assert ev["stages_done"] == FULL


def test_no_runs_at_all_fails(tmp_path):
    home = tmp_path / "home"
    (home / "runs").mkdir(parents=True)
    result = rs.validate(home, [], "I could not run it")
    assert result["ok"] is False
    assert result["run_id"] is None
    assert any("no run" in f for f in result["failures"])


def test_missing_stages_are_listed(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, ["new", "seeds", "discover_ats", "fetch"])
    result = rs.validate(home, _listing(home), RUN_A)
    assert result["ok"] is False
    missing = [f for f in result["failures"] if "stages" in f]
    assert missing and "filter" in missing[0] and "finalize" in missing[0]
    assert "seeds" not in missing[0].split("missing", 1)[1]


def test_empty_seeds_verified_fails(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, FULL, seeds_rows=0)
    result = rs.validate(home, _listing(home), RUN_A)
    assert result["ok"] is False
    assert any("seeds_verified.jsonl" in f for f in result["failures"])


def test_missing_seeds_verified_file_fails(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, FULL, seeds_rows=-1)
    result = rs.validate(home, _listing(home), RUN_A)
    assert result["ok"] is False
    assert any("seeds_verified.jsonl" in f for f in result["failures"])


def test_missing_results_md_fails(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, FULL, results=False)
    result = rs.validate(home, _listing(home), RUN_A)
    assert result["ok"] is False
    assert any("results.md" in f for f in result["failures"])


def test_run_id_not_reported_is_a_failure_but_run_is_still_checked(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, FULL)
    result = rs.validate(home, _listing(home), "All done, see results.md")
    assert result["ok"] is False
    assert result["run_id"] == RUN_A
    assert result["failures"] == ["agent output does not mention the run_id"]


def test_reported_run_wins_over_newer_incomplete_run(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, FULL)
    _make_run(home, RUN_B, ["new"], seeds_rows=-1, results=False)
    result = rs.validate(home, _listing(home), f"run {RUN_A} finished")
    assert result["ok"] is True, result
    assert result["run_id"] == RUN_A
    assert result["evidence"]["runs_total"] == 2


def test_unreported_run_picks_the_most_advanced(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, FULL)
    _make_run(home, RUN_B, ["new"], seeds_rows=-1, results=False)
    result = rs.validate(home, _listing(home), "no id here")
    assert result["run_id"] == RUN_A


def test_reported_run_that_does_not_exist_falls_back(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, FULL)
    result = rs.validate(home, _listing(home), "run 20990101-000000-ffffff done")
    assert result["run_id"] == RUN_A
    assert "agent output does not mention the run_id" in result["failures"]


def test_unattended_shortcut_is_flagged_in_evidence(tmp_path):
    home = tmp_path / "home"
    stages = ["new", "discover_feeds", "seeds", "discover_ats", "fetch", "filter", "finalize"]
    _make_run(home, RUN_A, stages, agent_files=())
    result = rs.validate(home, _listing(home), RUN_A)
    assert result["ok"] is True  # the brief's criteria only
    assert result["evidence"]["agentic"] is False
    assert result["evidence"]["agent_files"] == []


def test_find_run_ids():
    text = f"first {RUN_A}, then `{RUN_B}`; not 2026-11-16 nor 20261116-1015-abc"
    assert rs.find_run_ids(text) == [RUN_A, RUN_B]


def test_list_runs_via_cli_uses_the_real_cli(tmp_path):
    home = tmp_path / "home"
    _make_run(home, RUN_A, FULL)
    cmd = [sys.executable, "-c", "import sys; from ultron.cli import main; sys.exit(main(sys.argv[1:]))"]
    listing = rs.list_runs_via_cli(cmd, {"ULTRON_HOME": str(home)})
    assert [r["run_id"] for r in listing] == [RUN_A]
    assert listing[0]["stages_done"] == FULL


@pytest.mark.parametrize("platform", ["claude", "codex", "opencode", "antigravity"])
def test_child_env_scrubs_other_agents_and_sets_home(platform, tmp_path):
    base = {"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "x", "CODEX_SANDBOX": "seatbelt", "OPENCODE": "1",
            "ANTIGRAVITY_AGENT": "1", "OPENCODE_API_KEY": "k", "PATH": "p", "ULTRON_HOME": "real"}
    env = rs.child_env(platform, tmp_path / "home", base)
    assert env["ULTRON_HOME"] == str(tmp_path / "home")
    assert env["PATH"] == "p"
    for key in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CODEX_SANDBOX", "OPENCODE", "ANTIGRAVITY_AGENT"):
        assert key not in env
    assert ("OPENCODE_API_KEY" in env) == (platform == "opencode")
    assert "PWD" not in env


@pytest.mark.parametrize("platform", ["claude", "codex", "opencode", "antigravity"])
def test_child_env_is_an_allowlist(platform, tmp_path):
    secrets = {"FOO_TOKEN": "s1", "GITHUB_TOKEN": "s2", "ORCA_AGENT_HOOK_TOKEN": "s3", "ORCA_PANE_KEY": "s4",
               "ANTHROPIC_API_KEY": "s5", "OPENAI_API_KEY": "s6", "AWS_SECRET_ACCESS_KEY": "s7", "RANDOM_VAR": "x"}
    required = {"PATH": "p", "PATHEXT": ".EXE", "SystemRoot": "C:\\Windows", "WINDIR": "C:\\Windows",
                "COMSPEC": "cmd.exe", "TEMP": "t", "TMP": "t", "LANG": "es_ES.UTF-8", "LC_ALL": "C.UTF-8",
                "TERM": "xterm", "USERPROFILE": "C:\\Users\\u", "HOME": "/home/u", "APPDATA": "a",
                "LOCALAPPDATA": "l", "XDG_CONFIG_HOME": "x", "PSModulePath": "m"}
    env = rs.child_env(platform, tmp_path / "home", {**secrets, **required, "OPENCODE_API_KEY": "k"},
                       cwd=tmp_path / "project")
    for key in secrets:
        assert key not in env, key
    for key, value in required.items():
        assert env[key] == value, key
    assert env["ULTRON_HOME"] == str(tmp_path / "home")
    assert env["PWD"] == str(tmp_path / "project")
    assert ("OPENCODE_API_KEY" in env) == (platform == "opencode")
    assert set(env) <= set(required) | {"ULTRON_HOME", "PWD", "PYTHONIOENCODING", "OPENCODE_API_KEY"}


def test_child_env_points_pwd_at_the_project(tmp_path):
    env = rs.child_env("opencode", tmp_path / "home", {"PWD": "/c/elsewhere"}, cwd=tmp_path / "project")
    assert env["PWD"] == str(tmp_path / "project")


def test_build_command_shapes(tmp_path):
    project = tmp_path / "project"
    claude = rs.build_command("claude", "hi", project, model=None)
    assert Path(claude[0]).stem.lower() == "claude"
    assert "-p" in claude and "--strict-mcp-config" in claude and "--no-session-persistence" in claude
    assert "bypassPermissions" not in claude and "--dangerously-skip-permissions" not in claude
    codex = rs.build_command("codex", "hi", project, model=None)
    assert "exec" in codex and "workspace-write" in codex and "--ephemeral" in codex
    assert "--dangerously-bypass-approvals-and-sandbox" not in codex
    oc = rs.build_command("opencode", "hi", project, model=None)
    assert "run" in oc and "--auto" in oc and any(a.endswith("-free") for a in oc)
    agy = rs.build_command("antigravity", "hi", project, model=None)
    assert "--print" in agy or "-p" in agy
