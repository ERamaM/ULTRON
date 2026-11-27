"""``install.py --dev``: venv stub, skill links/copies and RUNTIME.md."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from ultron import __version__

REPO = Path(__file__).resolve().parents[1]


def _load_installer():
    spec = importlib.util.spec_from_file_location("ultron_install", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def installer(fake_home):
    return _load_installer()


def _fake_source(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "ultron"
    (src / "references").mkdir(parents=True)
    (src / "SKILL.md").write_text("---\nname: ultron\ndescription: x\n---\n", encoding="utf-8")
    (src / "references" / "seeds.md").write_text("seeds", encoding="utf-8")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "junk.pyc").write_bytes(b"x")
    return src


def test_install_dev_creates_targets(tmp_path):
    # --copy keeps the real skill/ultron untouched even where symlinks are allowed (CI on Linux/macOS).
    env = {
        **os.environ,
        "HOME": str(tmp_path / "user"),
        "USERPROFILE": str(tmp_path / "user"),
        "ULTRON_HOME": str(tmp_path / "home"),
        "ULTRON_INSTALL_SKIP_VENV": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(REPO / "install.py"), "--dev", "--target", str(tmp_path), "--copy"],
        capture_output=True, text=True, encoding="utf-8", env=env, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert len(json.loads(proc.stdout)["targets"]) == 2  # stdout is exactly one JSON object
    for base in (".claude", ".agents"):
        installed = tmp_path / base / "skills" / "ultron"
        assert (installed / "SKILL.md").is_file()
        assert (installed / "references" / "evaluation.md").is_file()
        runtime = (installed / "RUNTIME.md").read_text(encoding="utf-8")
        assert str(tmp_path / "home" / "venv") in runtime
        assert '& "' in runtime  # tells PowerShell users to quote the executable path


def test_old_python_is_rejected_with_json(installer, capsys, monkeypatch):
    monkeypatch.setattr(installer.sys, "version_info", (3, 11, 9))
    assert installer.main(["--dev"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["error"] == "python_too_old"


def _fake_popen(record):
    """A stand-in for subprocess.Popen whose steps succeed at once; ``record(cmd, kwargs)`` sees each call."""

    class FakePopen:
        pid = 0

        def __init__(self, cmd, **kwargs):
            record(cmd, kwargs)

        def wait(self):
            return 0

    return FakePopen


def test_venv_tool_output_goes_to_stderr(installer, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(installer.subprocess, "Popen", _fake_popen(lambda cmd, kwargs: calls.append(kwargs)))
    for uv in ("uv", None):
        monkeypatch.setattr(installer, "find_on_path", lambda name, uv=uv: uv)
        installer.create_venv(tmp_path / "venv", tmp_path / "scripts")
    assert len(calls) == 4
    assert all(kw.get("stdout") is sys.stderr for kw in calls)


def test_project_skill_folders_are_git_ignored():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    for path in (".claude/skills/ultron", ".agents/skills/ultron"):
        proc = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO, capture_output=True, check=False)
        assert proc.returncode == 0, path


def test_runtime_points_at_os_specific_executable(installer, tmp_path):
    exe = installer.ultron_executable(tmp_path / "home" / "venv")
    if os.name == "nt":
        assert exe == tmp_path / "home" / "venv" / "Scripts" / "ultron.exe"
    else:
        assert exe == tmp_path / "home" / "venv" / "bin" / "ultron"


def test_copy_mode_skips_caches_and_leaves_source_untouched(installer, tmp_path):
    src = _fake_source(tmp_path)
    target = tmp_path / "proj"
    report = installer.install_dev(target=target, source=src, force_copy=True)
    for base in (".claude", ".agents"):
        installed = target / base / "skills" / "ultron"
        assert not installed.is_symlink()
        assert (installed / "SKILL.md").is_file()
        assert (installed / "RUNTIME.md").is_file()
        assert not (installed / "__pycache__").exists()
    assert not (src / "RUNTIME.md").exists()
    assert {entry["mode"] for entry in report["targets"]} == {"copy"}


def test_symlink_mode_or_fallback(installer, tmp_path):
    src = _fake_source(tmp_path)
    report = installer.install_dev(target=tmp_path / "proj", source=src)
    for entry in report["targets"]:
        installed = Path(entry["path"])
        assert (installed / "SKILL.md").is_file()
        assert (installed / "RUNTIME.md").is_file()
        assert entry["mode"] in {"symlink", "copy"}
        assert installed.is_symlink() == (entry["mode"] == "symlink")


def test_symlink_failure_falls_back_to_copy(installer, tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise OSError("symlink privilege not held")

    monkeypatch.setattr(installer.os, "symlink", deny)
    src = _fake_source(tmp_path)
    report = installer.install_dev(target=tmp_path / "proj", source=src)
    assert {entry["mode"] for entry in report["targets"]} == {"copy"}
    assert not (src / "RUNTIME.md").exists()


def test_reinstall_replaces_previous_install_and_never_deletes_through_a_link(installer, tmp_path):
    src = _fake_source(tmp_path)
    target = tmp_path / "proj"
    installer.install_dev(target=target, source=src)
    installer.install_dev(target=target, source=src, force_copy=True)
    installer.install_dev(target=target, source=src)
    installer.install_dev(target=target, source=src)
    assert (src / "SKILL.md").is_file()
    assert (src / "references" / "seeds.md").is_file()
    assert (target / ".claude" / "skills" / "ultron" / "SKILL.md").is_file()


def test_refuses_to_replace_foreign_folder(installer, tmp_path):
    src = _fake_source(tmp_path)
    foreign = tmp_path / "proj" / ".claude" / "skills" / "ultron"
    foreign.mkdir(parents=True)
    (foreign / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(installer.InstallError):
        installer.install_dev(target=tmp_path / "proj", source=src)
    assert (foreign / "notes.txt").read_text(encoding="utf-8") == "mine"


def test_runtime_md_is_git_ignored():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "skill/ultron/RUNTIME.md"], cwd=REPO, capture_output=True, check=False
    )
    assert proc.returncode == 0


def test_dev_marker_in_source_is_git_ignored():
    if shutil.which("git") is None:
        pytest.skip("git not available")
    proc = subprocess.run(
        ["git", "check-ignore", "-q", "skill/ultron/.ultron-install.json"], cwd=REPO, capture_output=True, check=False
    )
    assert proc.returncode == 0


# --------------------------------------------------------------------------------------------------
# Phase 4: global / project install, update and uninstall
# --------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Point HOME/USERPROFILE/ULTRON_HOME at tmp so nothing ever touches the real profile."""
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setenv("HOME", str(user))
    monkeypatch.setenv("USERPROFILE", str(user))
    monkeypatch.setenv("ULTRON_HOME", str(user / ".ultron"))
    monkeypatch.setenv("ULTRON_INSTALL_SKIP_VENV", "1")
    assert Path.home() == user  # USERPROFILE on Windows, HOME elsewhere
    return user


@pytest.fixture
def inst(fake_home):
    return _load_installer()


def _run_main(inst, capsys, *argv) -> tuple[int, dict]:
    code = inst.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _by_path(report: dict) -> dict[str, dict]:
    return {entry["path"]: entry for entry in report["targets"]}


def test_global_targets_per_platform(inst, fake_home):
    all4 = ["claude", "codex", "opencode", "antigravity"]
    assert inst.platform_targets("global", None, all4) == {
        "claude": fake_home / ".claude" / "skills" / "ultron",
        "codex": fake_home / ".agents" / "skills" / "ultron",
        "opencode": fake_home / ".config" / "opencode" / "skills" / "ultron",
        "antigravity": fake_home / ".gemini" / "config" / "skills" / "ultron",
    }
    (fake_home / ".gemini" / "antigravity").mkdir(parents=True)
    targets = inst.platform_targets("global", None, ["antigravity"])
    assert targets == {
        "antigravity": fake_home / ".gemini" / "config" / "skills" / "ultron",
        "antigravity-legacy": fake_home / ".gemini" / "antigravity" / "skills" / "ultron",
    }
    proj = fake_home / "proj"
    assert inst.platform_targets("project", proj, all4) == {
        "claude": proj / ".claude" / "skills" / "ultron",
        "codex": proj / ".agents" / "skills" / "ultron",
        "opencode": proj / ".agents" / "skills" / "ultron",
        "antigravity": proj / ".agents" / "skills" / "ultron",
    }


def test_default_platforms_follow_existing_base_folders(inst, fake_home):
    assert inst.detect_platforms() == ["claude", "codex", "opencode", "antigravity"]  # none present: all four
    (fake_home / ".claude").mkdir()
    (fake_home / ".codex").mkdir()
    assert inst.detect_platforms() == ["claude", "codex"]
    (fake_home / ".config" / "opencode").mkdir(parents=True)
    (fake_home / ".gemini").mkdir()
    assert inst.detect_platforms() == ["claude", "codex", "opencode", "antigravity"]


def test_default_is_global_for_detected_platforms(inst, fake_home, capsys):
    (fake_home / ".claude").mkdir()
    code, report = _run_main(inst, capsys)
    assert code == 0, report
    assert report["scope"] == "global"
    assert [e["path"] for e in report["targets"]] == [str(fake_home / ".claude" / "skills" / "ultron")]
    installed = fake_home / ".claude" / "skills" / "ultron"
    assert (installed / "SKILL.md").is_file()
    assert (installed / "references" / "evaluation.md").is_file()
    assert not installed.is_symlink()
    assert not (fake_home / ".agents").exists()


def test_install_is_idempotent_and_removes_stale_files(inst, fake_home, capsys):
    dest = fake_home / ".claude" / "skills" / "ultron"
    code, first = _run_main(inst, capsys, "--platforms", "claude")
    assert code == 0 and first["targets"][0]["status"] == "installed"
    (dest / "stale.md").write_text("old", encoding="utf-8")
    (dest / "references" / "old_ref.md").write_text("old", encoding="utf-8")
    code, second = _run_main(inst, capsys, "--platforms", "claude")
    assert code == 0 and second["targets"][0]["status"] == "updated"
    assert not (dest / "stale.md").exists()
    (dest / "stale.md").write_text("old", encoding="utf-8")
    code, upd = _run_main(inst, capsys, "--update")
    assert code == 0
    entries = _by_path(upd)
    assert entries[str(dest)]["status"] == "updated"
    assert not (dest / "stale.md").exists()
    assert not (dest / "references" / "old_ref.md").exists()
    assert (dest / "SKILL.md").is_file()
    # --update refreshes existing installs only; it never creates new ones
    others = [e for p, e in entries.items() if p != str(dest)]
    assert others and all(e["status"] == "skipped" and e["reason"] == "not installed" for e in others)
    assert not (fake_home / ".agents" / "skills" / "ultron").exists()


def test_uninstall_only_marked_copies(inst, fake_home, capsys):
    code, _ = _run_main(inst, capsys, "--platforms", "claude,opencode")
    assert code == 0
    foreign = fake_home / ".agents" / "skills" / "ultron"
    foreign.mkdir(parents=True)
    (foreign / "SKILL.md").write_text("---\nname: ultron\ndescription: mine\n---\n", encoding="utf-8")
    (foreign / "notes.txt").write_text("mine", encoding="utf-8")
    code, report = _run_main(inst, capsys, "--uninstall")
    assert code == 0, report
    entries = _by_path(report)
    assert entries[str(fake_home / ".claude" / "skills" / "ultron")]["status"] == "removed"
    assert entries[str(fake_home / ".config" / "opencode" / "skills" / "ultron")]["status"] == "removed"
    assert entries[str(foreign)]["status"] == "skipped"
    assert (foreign / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert not (fake_home / ".claude" / "skills" / "ultron").exists()
    assert (fake_home / ".claude" / "skills").is_dir()  # parents are never removed
    assert inst.uninstall_copy(foreign) is False
    assert foreign.is_dir()


def test_uninstall_keeps_ultron_home_without_purge_flag(inst, fake_home, capsys):
    home = fake_home / ".ultron"
    (home / "runs").mkdir(parents=True)
    (home / ".ultron-home").write_text("x", encoding="utf-8")  # a home install.py created
    (home / "ultron.sqlite3").write_bytes(b"db")
    code, report = _run_main(inst, capsys, "--uninstall")
    assert code == 0
    assert report["ultron_home"]["status"] == "kept"
    assert "--purge-data" in report["ultron_home"]["hint"]
    assert (home / "ultron.sqlite3").is_file()
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 0
    assert report["ultron_home"]["status"] == "removed"
    assert not home.exists()
    assert fake_home.is_dir()


def test_purge_refuses_folder_that_is_not_an_ultron_home(inst, fake_home, capsys, monkeypatch):
    other = fake_home / "Documents"
    other.mkdir()
    (other / "thesis.docx").write_bytes(b"x")
    monkeypatch.setenv("ULTRON_HOME", str(other))
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 1
    assert report["ultron_home"]["status"] == "refused"
    assert (other / "thesis.docx").is_file()
    monkeypatch.setenv("ULTRON_HOME", str(fake_home))  # never the user's home itself
    (fake_home / "ultron.sqlite3").write_bytes(b"db")
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 1
    assert report["ultron_home"]["status"] == "refused"
    assert (other / "thesis.docx").is_file()


def test_install_skips_foreign_folder_with_reason(inst, fake_home, capsys):
    foreign = fake_home / ".claude" / "skills" / "ultron"
    foreign.mkdir(parents=True)
    (foreign / "notes.txt").write_text("mine", encoding="utf-8")
    code, report = _run_main(inst, capsys, "--platforms", "claude,codex")
    entries = _by_path(report)
    assert entries[str(foreign)]["status"] == "skipped"
    assert "not an ULTRON install" in entries[str(foreign)]["reason"]
    assert entries[str(fake_home / ".agents" / "skills" / "ultron")]["status"] == "installed"
    assert (foreign / "notes.txt").read_text(encoding="utf-8") == "mine"
    assert sorted(p.name for p in foreign.iterdir()) == ["notes.txt"]
    assert code == 1  # something was not installed


def test_legacy_phase1_install_is_upgraded(inst, fake_home, capsys):
    proj = fake_home / "proj"
    legacy = proj / ".claude" / "skills" / "ultron"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("---\nname: ultron\ndescription: x\n---\n", encoding="utf-8")
    (legacy / "RUNTIME.md").write_text("# ULTRON runtime\n\nold\n", encoding="utf-8")
    (legacy / "stale.md").write_text("old", encoding="utf-8")
    code, report = _run_main(inst, capsys, "--project", str(proj), "--platforms", "claude")
    assert code == 0, report
    assert report["targets"][0]["status"] == "upgraded-legacy"
    assert (legacy / ".ultron-install.json").is_file()
    assert not (legacy / "stale.md").exists()
    # P4-R4b: unmarked folders are never deleted, only moved to $ULTRON_HOME/backups
    (backup,) = (fake_home / ".ultron" / "backups").glob("legacy-skill-claude-*")
    assert report["targets"][0]["backup"] == str(backup)
    assert (backup / "stale.md").read_text(encoding="utf-8") == "old"
    assert sorted(p.name for p in legacy.parent.iterdir()) == ["ultron"]


def test_project_install_dedupes_shared_agents_folder(inst, fake_home, capsys):
    proj = fake_home / "proj"
    code, report = _run_main(inst, capsys, "--project", str(proj))
    assert code == 0, report
    assert report["scope"] == "project"
    entries = _by_path(report)
    assert set(entries) == {str(proj / ".claude" / "skills" / "ultron"), str(proj / ".agents" / "skills" / "ultron")}
    assert entries[str(proj / ".agents" / "skills" / "ultron")]["platforms"] == ["codex", "opencode", "antigravity"]
    for entry in report["targets"]:
        installed = Path(entry["path"])
        assert not installed.is_symlink()
        assert (installed / ".ultron-install.json").is_file()
    assert not (fake_home / ".claude").exists()


def test_path_with_spaces_and_accents(tmp_path):
    user = tmp_path / "José Pérez"
    user.mkdir()
    env = {
        **os.environ,
        "HOME": str(user),
        "USERPROFILE": str(user),
        "ULTRON_HOME": str(user / ".ultron"),
        "ULTRON_INSTALL_SKIP_VENV": "1",
    }
    proc = subprocess.run(
        [sys.executable, str(REPO / "install.py"), "--platforms", "claude,antigravity"],
        capture_output=True, text=True, encoding="utf-8", env=env, check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert len(proc.stdout.splitlines()) <= 40
    expected = [user / ".claude" / "skills" / "ultron", user / ".gemini" / "config" / "skills" / "ultron"]
    assert [Path(e["path"]) for e in report["targets"]] == expected
    for dest in expected:
        runtime = (dest / "RUNTIME.md").read_text(encoding="utf-8")
        assert str(user / ".ultron" / "venv") in runtime
        marker = json.loads((dest / ".ultron-install.json").read_text(encoding="utf-8"))
        assert marker["path"] == str(dest)


def test_runtime_md_points_to_executable(inst, fake_home, capsys):
    code, report = _run_main(inst, capsys, "--platforms", "codex")
    assert code == 0
    dest = fake_home / ".agents" / "skills" / "ultron"
    exe = inst.ultron_executable(fake_home / ".ultron" / "venv")
    assert report["ultron"] == str(exe)
    runtime = (dest / "RUNTIME.md").read_text(encoding="utf-8")
    assert f"```\n{exe}\n```" in runtime
    assert f'& "{exe}"' in runtime
    assert __version__ in runtime  # version
    marker = json.loads((dest / ".ultron-install.json").read_text(encoding="utf-8"))
    assert marker["installed_at"][:10] in runtime  # date
    assert marker["ultron"] == str(exe)
    assert marker["version"] == __version__
    assert marker["mode"] == "copy"


def test_dry_run_writes_nothing(inst, fake_home, capsys):
    before = sorted(fake_home.rglob("*"))
    code, report = _run_main(inst, capsys, "--dry-run")
    assert code == 0 and report["dry_run"] is True
    assert {e["status"] for e in report["targets"]} == {"installed"}
    assert sorted(fake_home.rglob("*")) == before
    code, _ = _run_main(inst, capsys, "--platforms", "claude")
    assert (fake_home / ".ultron" / ".ultron-home").is_file()  # install marks the home it uses
    before = sorted(fake_home.rglob("*"))
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data", "--dry-run")
    assert code == 0 and report["dry_run"] is True
    assert report["ultron_home"]["status"] == "removed"
    assert sorted(fake_home.rglob("*")) == before


def test_dev_symlink_uninstall_unlinks_without_touching_source(inst, fake_home, tmp_path):
    src = _fake_source(tmp_path)
    proj = tmp_path / "proj"
    inst.install_dev(target=proj, source=src)
    for base in (".claude", ".agents"):
        dest = proj / base / "skills" / "ultron"
        if dest.is_symlink():  # the marker is never written through the link into the source
            assert not (src / ".ultron-install.json").exists()
        else:
            assert (dest / ".ultron-install.json").is_file()
        assert inst.uninstall_copy(dest) is True
        assert not dest.exists() and not dest.is_symlink()
    assert (src / "SKILL.md").is_file()
    assert (src / "references" / "seeds.md").is_file()


def test_install_copy_reports_status_and_never_follows_links(inst, fake_home, tmp_path):
    src = _fake_source(tmp_path)
    dest = fake_home / "skills" / "ultron"
    runtime = inst.runtime_info(mode="copy")
    assert inst.install_copy(src, dest, runtime) == "installed"
    assert inst.install_copy(src, dest, runtime) == "updated"
    assert not dest.is_symlink()
    assert not (dest / "__pycache__").exists()
    assert not (src / ".ultron-install.json").exists()


@pytest.mark.parametrize(
    "argv",
    [["--global", "--project", "x"], ["--uninstall", "--update"], ["--platforms", "claude,vscode"], ["--purge-data"]],
)
def test_conflicting_flags_are_usage_errors(inst, capsys, argv):
    code, report = _run_main(inst, capsys, *argv)
    assert code == 2
    assert report["error"] == "usage"


def test_non_editable_venv_install_commands(inst, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(inst.subprocess, "Popen", _fake_popen(lambda cmd, kwargs: calls.append(cmd)))
    scripts = tmp_path / "José Pérez" / "scripts"
    for uv in ("uv", None):
        monkeypatch.setattr(inst, "find_on_path", lambda name, uv=uv: uv)
        calls.clear()
        inst.create_venv(tmp_path / "venv", scripts, editable=False)
        install = calls[-1]
        assert "-e" not in install
        assert any(arg.startswith(str(scripts)) and arg.endswith("[cv,xlsx]") for arg in install)
        if uv:
            assert "--reinstall-package" in install  # pick up changed source at the same version
        calls.clear()
        inst.create_venv(tmp_path / "venv", scripts, editable=True)
        assert "-e" in calls[-1]


def test_non_editable_wheel_bundles_data(tmp_path):
    """Non-dev installs are regular installs: the wheel must carry ``ultron/_data`` (phase-1 ruling R6)."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available")
    out = tmp_path / "dist"
    proc = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out), str(REPO / "skill" / "ultron" / "scripts")],
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    assert proc.returncode == 0, proc.stderr
    (wheel,) = out.glob("ultron-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "ultron/_data/domain_rules.yaml" in names
    assert "ultron/cli.py" in names


def test_user_made_link_to_a_marked_copy_is_not_ours(inst, fake_home, capsys):
    code, _ = _run_main(inst, capsys, "--platforms", "claude")
    assert code == 0
    real = fake_home / ".claude" / "skills" / "ultron"
    link = fake_home / ".agents" / "skills" / "ultron"
    link.parent.mkdir(parents=True)
    try:
        os.symlink(real, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not allowed here")
    code, report = _run_main(inst, capsys, "--platforms", "codex")
    assert _by_path(report)[str(link)]["status"] == "skipped"
    assert link.is_symlink()
    assert inst.uninstall_copy(link) is False
    assert link.is_symlink() and (real / "SKILL.md").is_file()
    code, report = _run_main(inst, capsys, "--uninstall", "--platforms", "codex")
    assert _by_path(report)[str(link)]["status"] == "skipped"
    assert link.is_symlink() and (real / "SKILL.md").is_file()


# ---- review round 1: deletion safety -------------------------------------------------------------


def _symlink_or_skip(target: Path, link: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not allowed here")


@pytest.fixture
def fake_repo(inst, tmp_path, monkeypatch):
    """A throw-away repo standing in for REPO/skill/ultron so a regression can never hit the real source."""
    repo = tmp_path / "repo"
    source = repo / "skill" / "ultron"
    shutil.copytree(_fake_source(tmp_path), source)
    (source / "scripts" / "ultron").mkdir(parents=True)
    (source / "scripts" / "ultron" / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
    (source / "RUNTIME.md").write_text("# ULTRON runtime\n\nold\n", encoding="utf-8")  # git-ignored dev leftover
    monkeypatch.setattr(inst, "REPO", repo)
    monkeypatch.setattr(inst, "SKILL_SOURCE", source)
    return source


def _source_intact(source: Path) -> bool:
    return (source / "SKILL.md").is_file() and (source / "references" / "seeds.md").is_file()


@pytest.mark.parametrize("planted", ["empty", "path"])
def test_skills_folder_linked_into_source_is_never_touched(inst, fake_home, fake_repo, capsys, planted):
    _symlink_or_skip(fake_repo.parent, fake_home / ".claude" / "skills")  # ~/.claude/skills -> <repo>/skill
    dst = fake_home / ".claude" / "skills" / "ultron"
    marker = {} if planted == "empty" else {"name": "ultron", "path": str(dst)}
    (fake_repo / ".ultron-install.json").write_text(json.dumps(marker), encoding="utf-8")
    for argv in (["--platforms", "claude"], ["--dev", "--global", "--platforms", "claude"],
                 ["--uninstall", "--platforms", "claude"], ["--update", "--platforms", "claude"]):
        _code, report = _run_main(inst, capsys, *argv)
        entry = _by_path(report)[str(dst)]
        assert entry["status"] == "skipped", (argv, entry)
        assert "dest_is_source" in entry["reason"]
        assert _source_intact(fake_repo), argv
    assert inst.uninstall_copy(dst) is False
    with pytest.raises(inst.InstallError, match="dest_is_source"):
        inst.install_copy(fake_repo, dst, inst.runtime_info())
    assert _source_intact(fake_repo)


def test_project_inside_source_tree_is_refused(inst, fake_home, fake_repo, capsys):
    code, report = _run_main(inst, capsys, "--project", str(fake_repo))
    assert code == 1
    assert all("dest_is_source" in e["reason"] for e in report["targets"])
    assert not (fake_repo / ".claude").exists()


def test_planted_or_copied_marker_does_not_make_a_folder_ours(inst, fake_home, capsys):
    empty = fake_home / ".claude" / "skills" / "ultron"
    empty.mkdir(parents=True)
    (empty / ".ultron-install.json").write_text("{}", encoding="utf-8")
    (empty / "notes.txt").write_text("mine", encoding="utf-8")
    elsewhere = fake_home / ".agents" / "skills" / "ultron"
    elsewhere.mkdir(parents=True)
    copied = {"name": "ultron", "path": str(fake_home / "somewhere" / "ultron")}
    (elsewhere / ".ultron-install.json").write_text(json.dumps(copied), encoding="utf-8")
    (elsewhere / "notes.txt").write_text("mine", encoding="utf-8")
    code, report = _run_main(inst, capsys, "--uninstall", "--platforms", "claude,codex")
    assert code == 0 and {e["status"] for e in report["targets"]} == {"skipped"}
    code, report = _run_main(inst, capsys, "--platforms", "claude,codex")
    assert code == 1 and {e["status"] for e in report["targets"]} == {"skipped"}
    for folder in (empty, elsewhere):
        assert (folder / "notes.txt").read_text(encoding="utf-8") == "mine"
        assert inst.uninstall_copy(folder) is False


def test_global_legacy_folder_is_moved_aside_not_deleted(inst, fake_home, capsys):
    legacy = fake_home / ".claude" / "skills" / "ultron"
    legacy.mkdir(parents=True)
    (legacy / "SKILL.md").write_text("---\nname: ultron\ndescription: x\n---\n", encoding="utf-8")
    (legacy / "RUNTIME.md").write_text("# ULTRON runtime\n\nold\n", encoding="utf-8")
    (legacy / "my-notes.md").write_text("precious", encoding="utf-8")
    code, report = _run_main(inst, capsys, "--dry-run", "--platforms", "claude")
    assert report["targets"][0]["status"] == "upgraded-legacy"
    assert not (fake_home / ".ultron").exists()
    code, report = _run_main(inst, capsys, "--platforms", "claude")
    assert code == 0 and report["targets"][0]["status"] == "upgraded-legacy"
    (backup,) = (fake_home / ".ultron" / "backups").iterdir()
    assert re.fullmatch(r"legacy-skill-claude-\d{8}-\d{6}", backup.name)
    assert report["targets"][0]["backup"] == str(backup)
    # nothing that looks like a second skill is left next to the install
    assert sorted(p.name for p in legacy.parent.iterdir()) == ["ultron"]
    assert (backup / "my-notes.md").read_text(encoding="utf-8") == "precious"
    assert not (legacy / "my-notes.md").exists()
    assert (legacy / ".ultron-install.json").is_file()


def test_purge_needs_home_marker_or_database(inst, fake_home, capsys, monkeypatch):
    venv_only = fake_home / "proj-venv"
    (venv_only / "venv").mkdir(parents=True)
    (venv_only / "venv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (venv_only / "main.py").write_text("print('mine')\n", encoding="utf-8")
    runs_only = fake_home / "experiments"
    (runs_only / "runs").mkdir(parents=True)
    for folder in (venv_only, runs_only):
        monkeypatch.setenv("ULTRON_HOME", str(folder))
        code, report = _run_main(inst, capsys, "--uninstall", "--purge-data")
        assert code == 1 and report["ultron_home"]["status"] == "refused", folder
        assert folder.is_dir()
    assert (venv_only / "main.py").is_file()
    real = fake_home / ".ultron"
    monkeypatch.setenv("ULTRON_HOME", str(real))
    code, _ = _run_main(inst, capsys, "--platforms", "claude")  # writes .ultron-home
    assert (real / ".ultron-home").is_file()
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 0 and report["ultron_home"]["status"] == "removed"
    assert not real.exists()


def test_swap_failure_restores_previous_install(inst, fake_home, tmp_path, monkeypatch):
    src = _fake_source(tmp_path)
    dest = fake_home / "skills" / "ultron"
    runtime = inst.runtime_info()
    inst.install_copy(src, dest, runtime)
    (dest / "old-only.md").write_text("old", encoding="utf-8")
    real_rename = Path.rename

    def flaky_rename(self, target):
        if self.name == "ultron" and self.parent.name.startswith(".ultron-new-"):
            raise OSError("simulated failure moving the new copy into place")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)
    with pytest.raises(OSError, match="simulated"):
        inst.install_copy(src, dest, runtime)
    monkeypatch.setattr(Path, "rename", real_rename)
    assert (dest / "old-only.md").read_text(encoding="utf-8") == "old"
    assert (dest / ".ultron-install.json").is_file()
    assert not list(dest.parent.glob(".ultron-new-*"))


def _inherited_everyone_read(path: Path) -> bool:
    # Read rules as SIDs so the check does not depend on account-name resolution.
    escaped_path = str(path).replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$acl = [IO.File]::GetAccessControl('{escaped_path}'); "
        "@($acl.GetAccessRules($false, $true, [Security.Principal.SecurityIdentifier]) | Where-Object { "
        "$_.IdentityReference.Value -eq 'S-1-1-0' -and $_.AccessControlType -eq 'Allow' -and "
        "($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadData) }).Count"
    )
    out = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True, check=False)
    assert out.returncode == 0, out.stderr
    return int(out.stdout.strip()) > 0


def test_installed_skill_inherits_the_parent_permissions(inst, fake_home, tmp_path):
    # tempfile.mkdtemp makes a private folder (0o700; on Windows a protected owner-only ACL since Python 3.12.4).
    # A staged copy moved into place kept it, so other accounts (e.g. Codex's Windows sandbox user) could not
    # read SKILL.md or the references.
    src = _fake_source(tmp_path)
    parent = fake_home / "skills"
    parent.mkdir(parents=True)
    dest = parent / "ultron"
    if os.name == "nt":
        if shutil.which("powershell") is None or shutil.which("icacls") is None:
            pytest.skip("powershell/icacls not available")
        subprocess.run(["icacls", str(parent), "/grant", "*S-1-1-0:(OI)(CI)RX", "/Q"], capture_output=True, check=True)
        sibling = parent / "plain"
        sibling.mkdir()
        sample = sibling / "sample.md"
        sample.touch()
        assert _inherited_everyone_read(sample), "A regular file did not inherit the test ACL"
        inst.install_copy(src, dest, inst.runtime_info())
        for installed_file in (dest / "SKILL.md", dest / "references" / "seeds.md"):
            assert _inherited_everyone_read(installed_file), f"{installed_file} did not inherit the destination ACL"
    else:
        inst.install_copy(src, dest, inst.runtime_info())
        sibling = parent / "plain"
        sibling.mkdir()
        assert (dest.stat().st_mode & 0o777) == (sibling.stat().st_mode & 0o777)


def test_unrelated_staging_folders_are_preserved(inst, fake_home, tmp_path):
    src = _fake_source(tmp_path)
    parent = fake_home / "skills"
    stale = parent / ".ultron-new-abc_123"
    (stale / "ultron").mkdir(parents=True)
    (stale / "ultron" / "sentinel").write_text("unowned", encoding="utf-8")
    active = inst._make_staging(parent)
    (active / "sentinel").write_text("in progress", encoding="utf-8")
    lookalike = parent / ".ultron-new-abc.keep"
    lookalike.mkdir()
    other = parent / "ultron-new-x"
    other.mkdir()
    inst.install_copy(src, parent / "ultron", inst.runtime_info())
    assert (stale / "ultron" / "sentinel").read_text(encoding="utf-8") == "unowned"
    assert (active / "sentinel").read_text(encoding="utf-8") == "in progress"
    assert lookalike.is_dir() and other.is_dir()


def _make_legacy(folder: Path) -> None:
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text("---\nname: ultron\ndescription: x\n---\n", encoding="utf-8")
    (folder / "RUNTIME.md").write_text("# ULTRON runtime\n\nold\n", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "my-notes.md").write_text("precious", encoding="utf-8")


def _cross_device_for_backups(real_rename):
    """os.rename that fails like a cross-drive move, but only for the legacy backup."""

    def rename(a, b, *args, **kwargs):
        if Path(b).name.startswith("legacy-skill-"):
            raise OSError(18, "Invalid cross-device link")
        return real_rename(a, b, *args, **kwargs)

    return rename


def test_legacy_backup_across_drives_copies_verifies_then_deletes(inst, fake_home, tmp_path, monkeypatch):
    legacy = fake_home / ".claude" / "skills" / "ultron"
    _make_legacy(legacy)

    monkeypatch.setattr(inst.os, "rename", _cross_device_for_backups(inst.os.rename))
    info: dict = {}
    status = inst.install_copy(_fake_source(tmp_path), legacy, inst.runtime_info(), label="claude", info=info)
    assert status == "upgraded-legacy"
    backup = Path(info["backup"])
    assert backup.parent == fake_home / ".ultron" / "backups"
    assert (backup / "sub" / "my-notes.md").read_text(encoding="utf-8") == "precious"
    assert (legacy / ".ultron-install.json").is_file()
    assert not (legacy / "sub").exists()


def test_legacy_backup_that_does_not_verify_keeps_the_original(inst, fake_home, tmp_path, monkeypatch):
    legacy = fake_home / ".claude" / "skills" / "ultron"
    _make_legacy(legacy)
    monkeypatch.setattr(inst.os, "rename", _cross_device_for_backups(inst.os.rename))
    real_copytree = shutil.copytree

    def lossy_copytree(src, dst, *args, **kwargs):
        result = real_copytree(src, dst, *args, **kwargs)
        if Path(dst).name.startswith("legacy-skill-"):  # top-level backup copy only
            (Path(dst) / "sub" / "my-notes.md").unlink()
        return result

    monkeypatch.setattr(inst.shutil, "copytree", lossy_copytree)
    with pytest.raises(inst.InstallError, match="did not verify"):
        inst.install_copy(_fake_source(tmp_path), legacy, inst.runtime_info(), label="claude")
    assert (legacy / "sub" / "my-notes.md").read_text(encoding="utf-8") == "precious"
    assert not list((fake_home / ".ultron" / "backups").iterdir())


def test_platform_table_matches_doctor(installer, fake_home):
    """install.py keeps its own copy of the global paths; doctor's table must not drift from it."""
    from ultron.infra.platforms import global_skill_dirs, project_skill_dirs

    (Path.home() / ".gemini" / "antigravity").mkdir(parents=True)
    expected = global_skill_dirs(Path.home())
    names = ["claude", "codex", "opencode", "antigravity"]
    assert installer.platform_targets("global", None, names) == {
        k: v for k, v in expected.items()
    }
    proj = Path.home() / "proj"
    targets = installer.platform_targets("project", proj, names)
    assert set(targets.values()) == set(project_skill_dirs(proj).values())


# ---------------------------------------------------------------- C1: purge only deletes ULTRON-owned entries


def _ultron_layout(home: Path) -> None:
    for sub in ("venv", "runs", "profiles", "seeds", "backups", "cv"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    (home / "runs" / "20261116-120000-abcdef").mkdir()
    for name in ("ultron.sqlite3", "ultron.sqlite3-wal", "ultron.sqlite3-shm", "installs.json"):
        (home / name).write_bytes(b"x")


def test_install_does_not_mark_existing_non_empty_folder(inst, fake_home, capsys, monkeypatch):
    other = fake_home / "Documents"
    other.mkdir()
    (other / "thesis.docx").write_bytes(b"x")
    monkeypatch.setenv("ULTRON_HOME", str(other))
    code, _ = _run_main(inst, capsys, "--platforms", "claude")
    assert code == 0
    assert not (other / ".ultron-home").exists()
    empty = fake_home / "empty-home"
    empty.mkdir()
    monkeypatch.setenv("ULTRON_HOME", str(empty))
    _run_main(inst, capsys, "--platforms", "claude")
    assert (empty / ".ultron-home").is_file()  # an empty folder becomes an ULTRON home


def test_purge_with_marker_keeps_foreign_entries(inst, fake_home, capsys, monkeypatch):
    home = fake_home / "shared"
    _ultron_layout(home)
    (home / ".ultron-home").write_text("x", encoding="utf-8")
    (home / "thesis.docx").write_bytes(b"precious")
    (home / "photos").mkdir()
    owned = {"cover-letters", "salary", "watch", "toolkit"}
    for folder in owned:
        (home / folder).mkdir()
        (home / folder / "synthetic.txt").write_text("report", encoding="utf-8")
    monkeypatch.setenv("ULTRON_HOME", str(home))
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data", "--dry-run")
    assert code == 0
    assert owned <= set(report["ultron_home"]["would_delete"])
    assert report["ultron_home"]["kept_foreign_entries"] == ["photos", "thesis.docx"]
    assert (home / "ultron.sqlite3").is_file()
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 0 and report["ok"] is True
    data = report["ultron_home"]
    assert data["kept_foreign_entries"] == ["photos", "thesis.docx"]
    assert data["status"] != "removed"
    assert (home / "thesis.docx").read_bytes() == b"precious"
    assert (home / "photos").is_dir()
    assert sorted(p.name for p in home.iterdir()) == ["photos", "thesis.docx"]


def test_purge_with_database_keeps_foreign_entries(inst, fake_home, capsys, monkeypatch):
    home = fake_home / "work"
    home.mkdir()
    (home / "ultron.sqlite3").write_bytes(b"db")
    (home / "main.py").write_text("print('mine')\n", encoding="utf-8")
    monkeypatch.setenv("ULTRON_HOME", str(home))
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 0
    assert report["ultron_home"]["kept_foreign_entries"] == ["main.py"]
    assert (home / "main.py").is_file()
    assert not (home / "ultron.sqlite3").exists()


def test_purge_removes_a_real_ultron_home_completely(inst, fake_home, capsys):
    home = fake_home / ".ultron"
    code, _ = _run_main(inst, capsys, "--platforms", "claude")
    _ultron_layout(home)
    (home / "ultron.sqlite3-journal").write_bytes(b"j")
    (home / "update-notices").mkdir()
    (home / "update-notices" / "last-checked").touch()
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 0 and report["ultron_home"]["status"] == "removed"
    assert "kept_foreign_entries" not in report["ultron_home"]
    assert not home.exists()


def test_claude_copy_gets_argument_hint_others_keep_portable_frontmatter(inst, fake_home, capsys):
    proj = fake_home / "proj"
    code, report = _run_main(inst, capsys, "--project", str(proj))
    assert code == 0, report
    claude_md = (proj / ".claude" / "skills" / "ultron" / "SKILL.md").read_text(encoding="utf-8")
    agents_md = (proj / ".agents" / "skills" / "ultron" / "SKILL.md").read_text(encoding="utf-8")
    front = claude_md.split("---")[1]
    assert 'argument-hint: "[search|profile|jobs|runs|system] [action] [args]"' in front
    assert "argument-hint" not in agents_md
    assert agents_md == (REPO / "skill" / "ultron" / "SKILL.md").read_text(encoding="utf-8")
    # an update keeps exactly one hint line
    code, _ = _run_main(inst, capsys, "--project", str(proj), "--update")
    assert code == 0
    again = (proj / ".claude" / "skills" / "ultron" / "SKILL.md").read_text(encoding="utf-8")
    assert again.count("argument-hint:") == 1


def test_termination_signal_stops_a_running_step_now(installer, monkeypatch):
    import signal

    registered = {}
    monkeypatch.setattr(installer.signal, "signal", lambda signum, handler: registered.update({signum: handler}))
    monkeypatch.setitem(installer._TERMINATION, "code", 0)
    installer._handle_termination_signals()
    handler = registered[signal.SIGTERM]

    class Step:
        pid = 0

        def __init__(self, cmd, **kwargs):
            self.stopped = False

        def wait(self):
            handler(signal.SIGTERM, None)  # the signal arrives while the step runs

    stopped = []
    monkeypatch.setattr(installer.subprocess, "Popen", Step)
    monkeypatch.setattr(installer, "_stop_step", stopped.append)
    with pytest.raises(installer.Terminated) as exc:
        installer._run(["pip", "install"])
    assert exc.value.code == 128 + signal.SIGTERM
    assert installer._TERMINATION["in_step"] is False
    assert len(stopped) == 1  # the running step was stopped before the exception left _run


def test_termination_signal_outside_a_step_waits_for_the_next_step(installer, monkeypatch):
    import signal

    monkeypatch.setitem(installer._TERMINATION, "code", 0)
    installer._on_termination_signal(signal.SIGTERM, None)  # e.g. while a previous install is being replaced
    calls = []
    monkeypatch.setattr(installer.subprocess, "Popen", lambda cmd, **kwargs: calls.append(cmd))
    with pytest.raises(installer.Terminated) as exc:
        installer._run(["pip", "install"])
    assert exc.value.code == 128 + signal.SIGTERM
    assert calls == []


def _preexisting_user_folders(home: Path) -> None:
    for rel in ("venv/x", "backups/x", "cv/mine/resume.pdf", "runs/x", "profiles/x", "seeds/x"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_bytes(b"precious")


def test_purge_with_only_database_keeps_generic_folders(inst, fake_home, capsys, monkeypatch):
    home = fake_home / "shared-home"
    _preexisting_user_folders(home)
    for name in ("ultron.sqlite3", "ultron.sqlite3-wal", "installs.json"):
        (home / name).write_bytes(b"x")
    (home / "update-notices").mkdir()
    monkeypatch.setenv("ULTRON_HOME", str(home))
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data", "--dry-run")
    assert code == 0
    data = report["ultron_home"]
    assert sorted(data["would_delete"]) == ["installs.json", "ultron.sqlite3", "ultron.sqlite3-wal", "update-notices"]
    assert data["kept_foreign_entries"] == ["backups", "cv", "profiles", "runs", "seeds", "venv"]
    assert (home / "ultron.sqlite3").is_file()
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 0 and report["ok"] is True
    assert report["ultron_home"]["kept_foreign_entries"] == ["backups", "cv", "profiles", "runs", "seeds", "venv"]
    for rel in ("venv/x", "backups/x", "cv/mine/resume.pdf", "runs/x", "profiles/x", "seeds/x"):
        assert (home / rel).read_bytes() == b"precious", rel
    assert sorted(p.name for p in home.iterdir()) == ["backups", "cv", "profiles", "runs", "seeds", "venv"]


def test_purge_removes_home_marker_last_so_a_failed_purge_can_be_retried(inst, fake_home, monkeypatch):
    home = fake_home / "marked-home"
    _preexisting_user_folders(home)
    (home / "ultron.sqlite3").write_bytes(b"db")
    (home / ".ultron-home").write_text("x", encoding="utf-8")
    real_rmtree = inst.shutil.rmtree

    def failing_rmtree(path, *args, **kwargs):
        if Path(path).name == "venv":
            raise PermissionError("venv is in use")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(inst.shutil, "rmtree", failing_rmtree)
    with pytest.raises(PermissionError):
        inst._purge_home(home, dry_run=False)
    assert (home / ".ultron-home").is_file()
    assert (home / "venv").is_dir()
    monkeypatch.setattr(inst.shutil, "rmtree", real_rmtree)
    assert inst._purge_home(home, dry_run=False) == {"status": "removed"}
    assert not home.exists()


# ------------------------------------------------------------------ install records in installs.json


def _records(fake_home) -> dict:
    data = json.loads((fake_home / ".ultron" / "installs.json").read_text(encoding="utf-8"))
    return data.get("installs", {})


def test_install_records_scope_path_platforms_version_mode(inst, fake_home, capsys, tmp_path):
    code, _ = _run_main(inst, capsys, "--platforms", "claude,opencode")
    assert code == 0
    project = tmp_path / "mi proyecto ñ"
    project.mkdir()
    code, _ = _run_main(inst, capsys, "--project", str(project), "--platforms", "codex,opencode")
    assert code == 0
    records = _records(fake_home)
    claude = records[str(fake_home / ".claude" / "skills" / "ultron")]
    assert claude["scope"] == "global" and claude["platforms"] == ["claude"] and claude["mode"] == "copy"
    assert claude["version"] == inst._skill_version() and claude["installed_at"]
    assert claude["path"] == str(fake_home / ".claude" / "skills" / "ultron") and claude.get("project") is None
    agents = records[str(project.resolve() / ".agents" / "skills" / "ultron")]
    assert agents["scope"] == "project" and agents["project"] == str(project.resolve())
    assert agents["platforms"] == ["codex", "opencode"]
    assert str(fake_home / ".config" / "opencode" / "skills" / "ultron") in records
    assert not list((fake_home / ".ultron").glob("*.tmp"))  # atomic write leaves no temp file


def test_update_rewrites_record_and_uninstall_removes_it(inst, fake_home, capsys, monkeypatch):
    _run_main(inst, capsys, "--platforms", "claude")
    path = str(fake_home / ".claude" / "skills" / "ultron")
    first = _records(fake_home)[path]
    monkeypatch.setattr(inst, "_skill_version", lambda source=None: "9.9.9")
    code, _ = _run_main(inst, capsys, "--update", "--platforms", "claude")
    assert code == 0
    assert _records(fake_home)[path]["version"] == "9.9.9" != first["version"]
    code, _ = _run_main(inst, capsys, "--uninstall", "--platforms", "claude")
    assert code == 0
    assert path not in _records(fake_home)


def test_old_registry_format_keeps_links_and_migrates(inst, fake_home, capsys):
    registry = fake_home / ".ultron" / "installs.json"
    registry.parent.mkdir(parents=True)
    old_links = {str(fake_home / "proj" / ".claude" / "skills" / "ultron"): str(fake_home / "checkout")}
    registry.write_text(json.dumps({"links": old_links}), encoding="utf-8")
    assert inst._read_registry() == old_links
    code, _ = _run_main(inst, capsys, "--platforms", "claude")
    assert code == 0
    data = json.loads(registry.read_text(encoding="utf-8"))
    assert data["links"] == old_links
    assert str(fake_home / ".claude" / "skills" / "ultron") in data["installs"]


def test_corrupt_registry_does_not_break_install(inst, fake_home, capsys):
    registry = fake_home / ".ultron" / "installs.json"
    registry.parent.mkdir(parents=True)
    registry.write_text("{not json", encoding="utf-8")
    code, _ = _run_main(inst, capsys, "--platforms", "claude")
    assert code == 0
    assert str(fake_home / ".claude" / "skills" / "ultron") in _records(fake_home)


def test_dry_run_records_nothing(inst, fake_home, capsys):
    code, _ = _run_main(inst, capsys, "--platforms", "claude", "--dry-run")
    assert code == 0
    assert not (fake_home / ".ultron" / "installs.json").exists()


def test_records_of_deleted_folders_are_dropped_on_the_next_write(inst, fake_home, capsys):
    _run_main(inst, capsys, "--platforms", "claude,opencode")
    opencode = fake_home / ".config" / "opencode" / "skills" / "ultron"
    shutil.rmtree(opencode)  # deleted by hand
    assert str(opencode) in _records(fake_home)
    code, _ = _run_main(inst, capsys, "--update", "--platforms", "claude")
    assert code == 0
    assert list(_records(fake_home)) == [str(fake_home / ".claude" / "skills" / "ultron")]


def test_inaccessible_recorded_folder_is_not_pruned(inst, fake_home, capsys, monkeypatch):
    _run_main(inst, capsys, "--platforms", "claude,opencode")
    opencode = fake_home / ".config" / "opencode" / "skills" / "ultron"
    real_lstat = inst.os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == opencode:
            raise PermissionError("denied")  # Path.exists() would swallow this on Python 3.14
        return real_lstat(path, *args, **kwargs)

    real_exists, real_is_symlink = Path.exists, Path.is_symlink
    monkeypatch.setattr(inst.os, "lstat", lstat)
    # Python 3.14 behaviour: Path.exists()/is_symlink() swallow the PermissionError and return False
    monkeypatch.setattr(Path, "exists", lambda self, **kw: False if self == opencode else real_exists(self, **kw))
    monkeypatch.setattr(Path, "is_symlink", lambda self: False if self == opencode else real_is_symlink(self))
    assert inst._record_target_exists({"path": str(opencode)}) is True
    assert inst._record_target_exists({"path": str(fake_home / "never-existed")}) is False
    code, _ = _run_main(inst, capsys, "--update", "--platforms", "claude")
    assert code == 0
    assert str(opencode) in _records(fake_home)


def test_termination_after_a_copy_keeps_its_record(inst, fake_home, capsys, monkeypatch):
    real_copy = inst.install_copy

    def copy_then_signal(*args, **kwargs):
        status = real_copy(*args, **kwargs)
        inst._TERMINATION["code"] = 143  # SIGTERM arrives while the copy finishes
        return status

    monkeypatch.setitem(inst._TERMINATION, "code", 0)
    monkeypatch.setattr(inst, "install_copy", copy_then_signal)
    code = inst.main(["--platforms", "claude,opencode"])
    out = json.loads(capsys.readouterr().out)
    assert code == 143 and "error" in out
    records = _records(fake_home)
    assert list(records) == [str(fake_home / ".claude" / "skills" / "ultron")]  # stopped before the next target
    assert not list((fake_home / ".ultron").glob("*.tmp"))


def test_pending_termination_stops_install_before_the_next_target(inst, fake_home, capsys, monkeypatch):
    monkeypatch.setitem(inst._TERMINATION, "code", 143)
    proj = fake_home / "proj"
    code, report = _run_main(inst, capsys, "--project", str(proj))
    assert code == 143 and report["error"] == "terminated"
    assert not (proj / ".claude" / "skills" / "ultron").exists()


def test_pending_termination_never_starts_a_data_purge(inst, fake_home, capsys, monkeypatch):
    home = fake_home / ".ultron"
    _run_main(inst, capsys, "--platforms", "claude")
    _ultron_layout(home)
    monkeypatch.setitem(inst._TERMINATION, "code", 143)
    code, report = _run_main(inst, capsys, "--uninstall", "--purge-data", "--platforms", "codex")
    assert code == 143 and report == {"error": "terminated", "detail": report["detail"]}
    assert (home / "ultron.sqlite3").is_file()


def test_termination_during_a_step_reports_one_json_object(inst, fake_home, capsys, monkeypatch):
    def stopped(**kwargs):
        raise inst.Terminated(143)

    monkeypatch.setattr(inst, "run_install", stopped)
    code, report = _run_main(inst, capsys, "--project", str(fake_home / "proj"))
    assert code == 143 and report["error"] == "terminated"


def test_run_raises_called_process_error_on_failure(installer):
    with pytest.raises(subprocess.CalledProcessError):
        installer._run([sys.executable, "-c", "raise SystemExit(3)"])
    installer._run([sys.executable, "-c", "pass"])


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_sigterm_during_a_step_stops_its_descendants(installer, tmp_path, monkeypatch):
    import signal
    import threading
    import time

    marker = tmp_path / "grandchild-finished"
    started = tmp_path / "grandchild-started"
    grandchild = (f"import pathlib, time; pathlib.Path({str(started)!r}).touch(); time.sleep(3); "
                  f"pathlib.Path({str(marker)!r}).touch()")
    child = f"import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', {grandchild!r}]); time.sleep(30)"
    monkeypatch.setitem(installer._TERMINATION, "code", 0)
    previous = signal.signal(signal.SIGTERM, installer._on_termination_signal)

    def terminate():
        while not started.exists():
            time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)

    try:
        threading.Thread(target=terminate, daemon=True).start()
        with pytest.raises(installer.Terminated):
            installer._run([sys.executable, "-c", child])
    finally:
        signal.signal(signal.SIGTERM, previous)
    time.sleep(4)
    assert not marker.exists()  # the grandchild (a build backend, say) was stopped with the step's group


def test_signal_while_a_step_starts_still_stops_that_step(installer, monkeypatch):
    import signal

    monkeypatch.setitem(installer._TERMINATION, "code", 0)
    stopped = []

    class Step:
        pid = 0

        def __init__(self, cmd, **kwargs):
            installer._on_termination_signal(signal.SIGTERM, None)  # arrives inside Popen: only recorded

        def wait(self):
            raise AssertionError("the step must not be waited for after a termination")

    monkeypatch.setattr(installer.subprocess, "Popen", Step)
    monkeypatch.setattr(installer, "_stop_step", stopped.append)
    with pytest.raises(installer.Terminated):
        installer._run(["pip", "install"])
    assert len(stopped) == 1 and installer._TERMINATION["in_step"] is False


def test_second_signal_during_step_cleanup_does_not_interrupt_it(installer, monkeypatch):
    import signal

    monkeypatch.setitem(installer._TERMINATION, "code", 0)
    events = []

    class Step:
        pid = 0

        def __init__(self, cmd, **kwargs):
            pass

        def wait(self):
            installer._on_termination_signal(signal.SIGTERM, None)

    def stop(proc):
        installer._on_termination_signal(signal.SIGTERM, None)  # a second signal during the grace period
        events.append("stopped")

    monkeypatch.setattr(installer.subprocess, "Popen", Step)
    monkeypatch.setattr(installer, "_stop_step", stop)
    with pytest.raises(installer.Terminated):
        installer._run(["pip", "install"])
    assert events == ["stopped"]  # cleanup ran to the end


def test_signal_right_after_marking_the_step_still_stops_it(installer, monkeypatch):
    import signal

    monkeypatch.setitem(installer._TERMINATION, "code", 0)
    stopped = []

    class Flag(dict):
        def __setitem__(self, key, value):
            super().__setitem__(key, value)
            if key == "in_step" and value is True:
                installer._on_termination_signal(signal.SIGTERM, None)  # arrives right after in_step = True

    class Step:
        pid = 0

        def __init__(self, cmd, **kwargs):
            pass

        def wait(self):
            raise AssertionError("the step must not be waited for after a termination")

    monkeypatch.setattr(installer, "_TERMINATION", Flag(code=0, in_step=False))
    monkeypatch.setattr(installer.subprocess, "Popen", Step)
    monkeypatch.setattr(installer, "_stop_step", stopped.append)
    with pytest.raises(installer.Terminated):
        installer._run(["pip", "install"])
    assert len(stopped) == 1


@pytest.mark.parametrize("metadata", ["runtime", "marker"])
def test_metadata_failure_preserves_previous_copy(inst, tmp_path, monkeypatch, metadata):
    src = _fake_source(tmp_path)
    dst = tmp_path / "skills" / "ultron"
    runtime = inst.runtime_info()
    inst.install_copy(src, dst, runtime)
    (dst / "old-only.md").write_text("old", encoding="utf-8")
    marker = (dst / inst.MARKER).read_bytes()
    real_write = Path.write_text
    real_marker = inst._write_utf8_in_dir

    def write_text(path, *args, **kwargs):
        if path.name == "RUNTIME.md" and metadata == "runtime":
            raise OSError("metadata write failed")
        return real_write(path, *args, **kwargs)

    def write_marker(*args):
        if metadata == "marker":
            raise OSError("metadata write failed")
        return real_marker(*args)

    monkeypatch.setattr(Path, "write_text", write_text)
    monkeypatch.setattr(inst, "_write_utf8_in_dir", write_marker)
    with pytest.raises(OSError, match="metadata write failed"):
        inst.install_copy(src, dst, runtime)
    assert (dst / "old-only.md").read_text(encoding="utf-8") == "old"
    assert (dst / inst.MARKER).read_bytes() == marker
    assert inst._has_marker(dst)


def test_relative_runtime_paths_are_stable(inst, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ULTRON_HOME", "relative-state")
    dst = tmp_path / "skills" / "ultron"
    runtime = inst.runtime_info()
    inst.install_copy(_fake_source(tmp_path), dst, runtime)
    monkeypatch.chdir(dst)
    expected_home = tmp_path / "relative-state"
    assert Path(runtime["home"]) == expected_home
    assert Path(runtime["executable"]) == inst.ultron_executable(expected_home / "venv")
    text = (dst / "RUNTIME.md").read_text(encoding="utf-8")
    assert str(expected_home) in text and str(runtime["executable"]) in text


def test_retry_preserves_failed_rollback_copy(inst, tmp_path, monkeypatch):
    src = _fake_source(tmp_path)
    dst = tmp_path / "skills" / "ultron"
    runtime = inst.runtime_info()
    inst.install_copy(src, dst, runtime)
    (dst / "old-only.md").write_text("recover me", encoding="utf-8")
    rename = Path.rename

    def fail_activation_and_restore(path, target):
        if path.parent.name.startswith(".ultron-new-"):
            raise OSError("simulated rename failure")
        return rename(path, target)

    with monkeypatch.context() as mp:
        mp.setattr(Path, "rename", fail_activation_and_restore)
        with pytest.raises(inst.InstallError, match="previous install kept"):
            inst.install_copy(src, dst, runtime)
    [recovery] = dst.parent.glob(".ultron-new-*/old/old-only.md")
    inst.install_copy(src, dst, runtime)
    assert recovery.read_text(encoding="utf-8") == "recover me"


@pytest.mark.parametrize("kind", ["installs", "links"])
def test_concurrent_registry_updates_keep_both_records(inst, tmp_path, monkeypatch, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    folders = [tmp_path / name for name in ("first", "second")]
    for folder in folders:
        folder.mkdir()
    first_read, second_started, second_read = Event(), Event(), Event()
    read = inst._read_registry_file

    def read_snapshot():
        snapshot = read()
        if current_thread().name == "first_0":
            first_read.set()
            assert second_started.wait(5)
            second_read.wait(0.5)
        elif current_thread().name == "second_0":
            second_read.set()
        return snapshot

    def write(folder):
        if kind == "links":
            inst._register_link(folder, tmp_path)
        else:
            inst._record_install(folder, scope="project", project=folder, platforms=["claude"], version="1.0.0")

    def second_write():
        second_started.set()
        write(folders[1])

    monkeypatch.setattr(inst, "_read_registry_file", read_snapshot)
    with ThreadPoolExecutor(1, thread_name_prefix="first") as first:
        pending_first = first.submit(write, folders[0])
        assert first_read.wait(5)
        with ThreadPoolExecutor(1, thread_name_prefix="second") as second:
            pending_second = second.submit(second_write)
            pending_first.result(timeout=10)
            pending_second.result(timeout=10)
    assert set(read()[kind]) == set(map(str, folders))


@pytest.mark.parametrize("layout", ["marked", "shared", "orphan", "shared-orphan"])
def test_installer_purge_recognizes_the_real_ledger_lock(inst, fake_home, capsys, monkeypatch, layout):
    from contextlib import closing

    from ultron.storage import ledger

    home = fake_home / "purge-ledger"
    home.mkdir()
    monkeypatch.setenv("ULTRON_HOME", str(home))
    db = home / "ultron.sqlite3"
    with closing(ledger.connect(db)):
        pass
    lock = ledger.lock_path(db)
    assert lock.is_file()
    if layout == "marked":
        (home / ".ultron-home").touch()
    if layout.startswith("shared"):
        (home / "notes.txt").write_text("keep", encoding="utf-8")
    if layout.endswith("orphan"):
        db.unlink()
    if layout == "shared-orphan":
        (home / "installs.json").write_text("unrelated file", encoding="utf-8")

    code, preview = _run_main(inst, capsys, "--uninstall", "--purge-data", "--dry-run")
    assert code == 0
    assert (lock.name in preview["ultron_home"]["would_delete"]) is not layout.startswith("shared")
    assert lock.is_file()
    previous_lock_stat = lock.stat()
    code, result = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 0
    if layout.startswith("shared"):
        foreign = ["installs.json", "notes.txt"] if layout == "shared-orphan" else ["notes.txt"]
        assert result["ultron_home"]["kept_foreign_entries"] == foreign
        assert (home / "notes.txt").read_text(encoding="utf-8") == "keep"
        assert not db.exists()
        assert lock.stat().st_ino == previous_lock_stat.st_ino
        with closing(ledger.connect(db)) as connection:
            assert connection.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
    else:
        assert result["ultron_home"]["status"] == "removed"
        assert not home.exists()


def test_installer_purge_keeps_the_ledger_while_a_connection_is_open(inst, fake_home, monkeypatch, capsys):
    from contextlib import closing

    from ultron.storage import ledger

    home = fake_home / "busy-ledger"
    home.mkdir()
    marker = home / ".ultron-home"
    marker.touch()
    db = home / "ultron.sqlite3"
    monkeypatch.setenv("ULTRON_HOME", str(home))
    with closing(ledger.connect(db)) as conn:
        code, result = _run_main(inst, capsys, "--uninstall", "--purge-data")
        assert code == 1
        assert "close concurrent ULTRON commands" in json.dumps(result)
        assert db.exists() and marker.exists()
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
    assert _run_main(inst, capsys, "--uninstall", "--purge-data")[0] == 0
    assert not home.exists()


def test_installer_purge_keeps_a_directory_named_like_the_ledger_lock(inst, fake_home, capsys, monkeypatch):
    home = fake_home / "shared"
    lock_dir = home / ".ultron.sqlite3.lock"
    lock_dir.mkdir(parents=True)
    data = lock_dir / "keep.txt"
    data.write_text("user data", encoding="utf-8")
    (home / ".ultron-home").touch()
    monkeypatch.setenv("ULTRON_HOME", str(home))
    code, result = _run_main(inst, capsys, "--uninstall", "--purge-data")
    assert code == 0
    assert result["ultron_home"]["kept_foreign_entries"] == [lock_dir.name]
    assert data.read_text(encoding="utf-8") == "user data"


def test_uninstall_of_an_unregistered_copy_does_not_create_registry(inst, fake_home, capsys):
    _, report = _run_main(inst, capsys, "--platforms", "claude")
    target = Path(report["targets"][0]["path"])
    home = fake_home / ".ultron"
    registry, lock = home / "installs.json", home / ".ultron-installs.lock"
    registry.unlink()
    lock.unlink()
    code, report = _run_main(inst, capsys, "--uninstall", "--platforms", "claude")
    assert code == 0 and report["targets"][0]["status"] == "removed"
    assert not target.exists()
    assert not registry.exists() and not lock.exists()


def test_unknown_registry_entries_do_not_rewrite_existing_records(inst, fake_home):
    home = fake_home / ".ultron"
    home.mkdir()
    registry = home / "installs.json"
    original = b'{"installs":{},"links":{}}'
    registry.write_bytes(original)
    unknown = fake_home / "unknown"
    inst._forget_install(unknown)
    inst._forget_link(unknown)
    assert registry.read_bytes() == original
    assert not (home / ".ultron-installs.lock").exists()


@pytest.mark.parametrize("becomes_shared", [False, True])
def test_complete_uninstall_retires_the_lock_before_unlinking(inst, fake_home, monkeypatch, capsys, becomes_shared):
    import sqlite3
    from contextlib import closing

    from ultron.storage import ledger

    home = fake_home / "retire-ledger"
    home.mkdir()
    (home / ".ultron-home").touch()
    db = home / "ultron.sqlite3"
    lock = ledger.lock_path(db)
    monkeypatch.setenv("ULTRON_HOME", str(home))
    with closing(ledger.connect(db)):
        pass

    real_remove = inst._remove_home_entry
    attempted = []

    def remove(path):
        if path == lock:
            attempted.append(path)
            with pytest.raises(sqlite3.OperationalError, match="file lock is retired"):
                ledger.connect(db)
            assert not db.exists()
            if len(attempted) == 1:
                raise PermissionError("lock file cannot be unlinked yet")
        real_remove(path)

    monkeypatch.setattr(inst, "_remove_home_entry", remove)
    command = ("--uninstall", "--purge-data")
    assert _run_main(inst, capsys, *command)[0] == 1
    assert attempted == [lock]
    assert lock.is_file() and (home / ".ultron-home").is_file()
    if becomes_shared:
        (home / "notes.txt").touch()
    assert _run_main(inst, capsys, *command)[0] == 0
    if becomes_shared:
        assert attempted == [lock]
        assert (home / "notes.txt").is_file() and lock.is_file()
        with closing(ledger.connect(db)) as conn:
            assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone() == (0,)
    else:
        assert attempted == [lock, lock] and not home.exists()
