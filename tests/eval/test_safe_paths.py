"""Every path the developer tools read or write must resolve inside an allowed folder."""

from __future__ import annotations

import json
import os

import pytest
import quality_benchmark as qb
import replay_run
import safe_paths


@pytest.fixture
def allowed(tmp_path, monkeypatch):
    root = tmp_path / "allowed"
    root.mkdir()
    monkeypatch.setattr(safe_paths, "default_roots", lambda: (str(root),))
    return root


def test_paths_inside_the_root_are_resolved(allowed):
    assert safe_paths.contained_path(allowed / "sub" / ".." / "a.json") == (allowed / "a.json").resolve()
    assert safe_paths.contained_path(allowed) == allowed.resolve()


@pytest.mark.parametrize("relative", ["../escape.json", "../allowed-evil/x.json", "sub/../../escape.json"])
def test_dot_dot_escapes_are_rejected(allowed, relative):
    with pytest.raises(safe_paths.UnsafePathError):
        safe_paths.contained_path(os.path.join(allowed, relative))


def test_absolute_paths_outside_the_root_are_rejected(allowed, tmp_path):
    with pytest.raises(safe_paths.UnsafePathError):
        safe_paths.contained_path(tmp_path / "elsewhere" / "report.json")


def test_symlinks_resolving_outside_the_root_are_rejected(allowed, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    link = allowed / "link.json"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available on this system")
    with pytest.raises(safe_paths.UnsafePathError):
        safe_paths.contained_path(link)


def test_default_roots_are_the_repository_and_the_temp_folder():
    roots = safe_paths.default_roots()
    assert safe_paths.REPO_ROOT in roots and len(roots) == 2


def test_benchmark_rejects_reports_and_outputs_outside_the_roots(allowed, tmp_path, capsys):
    outside = tmp_path / "baseline.json"
    outside.write_text(qb.BASELINE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    assert qb.main(["compare", str(outside), str(outside)]) == 2
    assert qb.main(["compare", str(allowed / ".." / "baseline.json")]) == 2
    assert qb.main(["run", "--output", str(tmp_path / "candidate.json")]) == 2
    assert not (tmp_path / "candidate.json").exists()
    errors = [json.loads(line)["error"] for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    assert len(errors) == 3 and all("outside the allowed folders" in e for e in errors)


def test_replay_rejects_reports_and_outputs_outside_the_roots(allowed, tmp_path, capsys):
    report = {"schema_version": replay_run.REPORT_SCHEMA_VERSION, "identities": {}}
    outside = tmp_path / "a.json"
    outside.write_text(json.dumps(report), encoding="utf-8")
    inside = allowed / "a.json"
    inside.write_text(json.dumps(report), encoding="utf-8")
    assert replay_run.main(["diff", str(outside), str(inside)]) == 2
    assert replay_run.main(["diff", str(allowed / ".." / "a.json"), str(inside)]) == 2
    assert replay_run.main(["diff", str(inside), str(inside)]) == 0
    capsys.readouterr()


def test_replay_rejects_folders_that_are_not_run_folders(tmp_path, capsys):
    folder = tmp_path / "not-a-run"
    folder.mkdir()
    (folder / "run.json").write_text("{}", encoding="utf-8")
    (folder / "postings.jsonl").write_text("", encoding="utf-8")
    assert replay_run.main(["replay", "--run-dir", str(folder)]) == 2
    assert "not a run folder" in json.loads(capsys.readouterr().out)["error"]
    run = tmp_path / "20990101-000000-abcdef"
    run.mkdir()
    assert replay_run.main(["replay", "--run-dir", str(run)]) == 2
    assert "required" in json.loads(capsys.readouterr().out)["error"]


def test_replay_rejects_run_files_linked_outside_the_folder(tmp_path, capsys):
    run = tmp_path / "20990101-000000-abcdef"
    run.mkdir()
    (run / "postings.jsonl").write_text("", encoding="utf-8")
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    try:
        (run / "run.json").symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not available on this system")
    assert replay_run.main(["replay", "--run-dir", str(run)]) == 2
    assert "outside the run folder" in json.loads(capsys.readouterr().out)["error"]


def test_replay_by_run_id_never_leaves_the_runs_folder(capsys):
    for run_id in ("../20990101-000000-abcdef", "20990101-000000-abcdef/../../x"):
        assert replay_run.main(["replay", "--run", run_id]) == 2
        assert "invalid run id" in json.loads(capsys.readouterr().out)["error"]
