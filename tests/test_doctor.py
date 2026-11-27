from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest

from ultron.cli import main
from ultron.diagnostics import doctor
from ultron.infra import installs
from ultron.infra.platforms import detect_platforms, global_skill_dirs
from ultron.stages import http_factory

PLATFORM_ENV = ("CLAUDECODE", "CODEX_SANDBOX", "CODEX_HOME", "OPENCODE")


@pytest.fixture(autouse=True)
def _clean_env(tmp_path, monkeypatch):
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in PLATFORM_ENV:
        monkeypatch.delenv(name, raising=False)
    import os

    for key in [k for k in os.environ if k.startswith("ANTIGRAVITY")]:
        monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)  # project-scope folders are looked up in the cwd
    monkeypatch.setattr(installs, "in_dev_checkout", lambda: False)
    return home


def _public_resolver(hostname, port):
    return ["93.184.216.34"]


def _ok_transport():
    return httpx.MockTransport(lambda request: httpx.Response(200, json={"jobs": []}))


def _install_marker(home: Path, *parts: str) -> Path:
    dest = home.joinpath(*parts)
    dest.mkdir(parents=True)
    (dest / ".ultron-install.json").write_text(json.dumps({"name": "ultron", "path": str(dest)}), encoding="utf-8")
    return dest


def _by_id(checks):
    return {c.id: c for c in checks}


def test_all_ok_with_mock_network(_clean_env):
    _install_marker(_clean_env, ".claude", "skills", "ultron")
    http_factory.set_test_overrides(transport=_ok_transport(), resolver=_public_resolver, sleep=lambda s: None)
    checks = doctor.run_checks()
    assert [c.id for c in checks] == [
        "python_version", "ultron_home_writable", "db_schema", "optional_extras",
        "skill_installed", "web_search", "network_dns", "network_https",
    ]
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_network_blocked_gives_platform_fix(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/somewhere")

    def blocked(hostname, port):
        raise OSError("blocked")

    http_factory.set_test_overrides(transport=_ok_transport(), resolver=blocked)
    checks = _by_id(doctor.run_checks())
    assert not checks["network_dns"].ok
    assert not checks["network_https"].ok
    assert "Codex" in checks["network_dns"].fix
    assert "platforms.md" in checks["network_https"].fix
    assert "Claude Code" not in checks["network_dns"].fix


def test_unknown_platform_lists_all_four():
    http_factory.set_test_overrides(transport=_ok_transport(), resolver=lambda h, p: [])
    fix = _by_id(doctor.run_checks())["network_dns"].fix
    for label in ("Claude Code", "Codex", "OpenCode", "Antigravity"):
        assert label in fix


def test_http_failure_reports_fix():
    http_factory.set_test_overrides(
        transport=httpx.MockTransport(lambda r: httpx.Response(403)), resolver=_public_resolver, sleep=lambda s: None
    )
    check = _by_id(doctor.run_checks())["network_https"]
    assert not check.ok and "403" in check.detail


def test_missing_extra_reported(monkeypatch):
    import importlib.util

    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: None if name == "openpyxl" else real(name, *a))
    check = _by_id(doctor.run_checks(network=False))["optional_extras"]
    assert not check.ok
    assert "openpyxl" in check.detail and "xlsx" in check.fix


def test_no_network_skips_network_checks():
    ids = [c.id for c in doctor.run_checks(network=False)]
    assert "network_dns" not in ids and "network_https" not in ids


def test_public_diagnostic_contains_known_statuses_without_local_details(monkeypatch, capsys):
    def checks(*, network):
        assert not network
        return [
            doctor.Check("python_version", True, "Python available"),
            doctor.Check("ultron_home_writable", False, "/home/sample/private/profile.yaml", "Private path"),
            doctor.Check("sample@example.invalid", True, "Unexpected check"),
        ]

    monkeypatch.setattr(doctor, "run_checks", checks)
    assert main(["doctor", "--public", "--no-network"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"ok", "ultron", "python", "os", "checks"}
    assert payload["ok"] is False
    assert payload["checks"] == [
        {"id": "python_version", "ok": True},
        {"id": "ultron_home_writable", "ok": False},
    ]


def test_public_diagnostic_suppresses_unexpected_exception_details(monkeypatch, capsys):
    def broken(*, network):
        raise PermissionError("/home/sample/private/profile.yaml")

    monkeypatch.setattr(doctor, "run_checks", broken)
    assert main(["doctor", "--public"]) == 1
    public = capsys.readouterr()
    assert json.loads(public.out) == {
        "error": "diagnostic_failed",
        "detail": "The public diagnostic could not be completed.",
    }
    assert public.err == ""


def test_db_schema_missing_is_ok_and_not_created(tmp_path):
    check = _by_id(doctor.run_checks(network=False))["db_schema"]
    assert check.ok and "not created" in check.detail
    assert not (tmp_path / "ultron_home" / "ultron.sqlite3").exists()


def test_db_schema_newer_fails(tmp_path):
    home = tmp_path / "ultron_home"
    home.mkdir()
    conn = sqlite3.connect(home / "ultron.sqlite3")
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    check = _by_id(doctor.run_checks(network=False))["db_schema"]
    assert not check.ok and "99" in check.detail


def test_skill_installed_states(_clean_env):
    check = _by_id(doctor.run_checks(network=False))["skill_installed"]
    assert not check.ok and "claude: missing" in check.detail

    foreign = _clean_env / ".agents" / "skills" / "ultron"
    foreign.mkdir(parents=True)
    check = _by_id(doctor.run_checks(network=False))["skill_installed"]
    assert not check.ok and "codex: foreign" in check.detail

    _install_marker(_clean_env, ".claude", "skills", "ultron")
    check = _by_id(doctor.run_checks(network=False))["skill_installed"]
    assert check.ok and "claude: installed" in check.detail and "codex: foreign" in check.detail


def test_marker_with_wrong_path_is_foreign(_clean_env):
    dest = _clean_env / ".claude" / "skills" / "ultron"
    dest.mkdir(parents=True)
    (dest / ".ultron-install.json").write_text(json.dumps({"name": "ultron", "path": "/elsewhere"}), encoding="utf-8")
    check = _by_id(doctor.run_checks(network=False))["skill_installed"]
    assert not check.ok and "claude: foreign" in check.detail


def test_platform_detection():
    assert detect_platforms({"CLAUDECODE": "1"}) == ["claude"]
    assert detect_platforms({"CODEX_SANDBOX": "seatbelt"}) == ["codex"]
    assert detect_platforms({"OPENCODE": "1"}) == ["opencode"]
    assert detect_platforms({"ANTIGRAVITY_AGENT": "1"}) == ["antigravity"]
    assert detect_platforms({"PATH": "x"}) == []


def test_global_paths(tmp_path):
    dirs = global_skill_dirs(tmp_path)
    assert dirs["codex"] == tmp_path / ".agents" / "skills" / "ultron"
    assert dirs["antigravity"] == tmp_path / ".gemini" / "config" / "skills" / "ultron"
    assert "antigravity-legacy" not in dirs
    (tmp_path / ".gemini" / "antigravity").mkdir(parents=True)
    assert "antigravity-legacy" in global_skill_dirs(tmp_path)


def test_cli_doctor_json_and_exit_code(capsys):
    code = main(["doctor", "--no-network"])
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"ok", "checks"}
    assert payload["ok"] is False  # no skill installed in the isolated home
    assert code == 1


def test_cli_doctor_exit_zero_when_ok(_clean_env, capsys):
    _install_marker(_clean_env, ".claude", "skills", "ultron")
    assert main(["doctor", "--no-network"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_platforms_doc_has_all_sections():
    doc = Path(__file__).resolve().parents[1] / "skill" / "ultron" / "references" / "platforms.md"
    text = doc.read_text(encoding="utf-8")
    for label in ("Claude Code", "Codex", "OpenCode", "Antigravity"):
        assert f"## {label}" in text
    assert text.count("verified on") >= 4


def test_project_install_recognised(tmp_path):
    _install_marker(tmp_path, ".agents", "skills", "ultron")
    check = _by_id(doctor.run_checks(network=False))["skill_installed"]
    assert check.ok and "project .agents: installed" in check.detail
    assert "global claude: missing" in check.detail


def test_dev_checkout_without_install_is_informational(monkeypatch):
    monkeypatch.setattr(installs, "in_dev_checkout", lambda: True)
    check = _by_id(doctor.run_checks(network=False))["skill_installed"]
    assert check.ok and "source checkout" in check.detail and "install.py" in check.fix


def test_real_dev_checkout_detection(monkeypatch, tmp_path):
    monkeypatch.undo()  # drop the autouse stub of in_dev_checkout
    repo = Path(__file__).resolve().parents[1]
    assert installs.in_dev_checkout() is True

    import shutil

    copy = tmp_path / "home" / ".claude" / "skills" / "ultron"
    shutil.copytree(repo / "skill" / "ultron", copy, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (copy / ".ultron-install.json").write_text(json.dumps({"name": "ultron", "path": str(copy)}), encoding="utf-8")
    assert installs.in_dev_checkout(copy / "scripts" / "ultron") is False
    (copy / ".ultron-install.json").unlink()
    assert installs.in_dev_checkout(copy / "scripts" / "ultron") is False  # no install.py above


@pytest.mark.parametrize("kind", ["symlink", pytest.param("junction", marks=pytest.mark.skipif(
    os.name != "nt", reason="Windows junctions only"
))])
def test_only_registered_link_must_point_to_a_live_directory(tmp_path, _clean_env, capsys, kind):
    import subprocess

    source = tmp_path / "checkout"
    source.mkdir()
    link = _clean_env / ".claude" / "skills" / "ultron"
    link.parent.mkdir(parents=True)
    if kind == "junction":
        process = subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(source)], capture_output=True, check=False
        )
        if process.returncode:
            pytest.skip("junction creation is unavailable")
    else:
        try:
            link.symlink_to(source, target_is_directory=True)
        except OSError:
            pytest.skip("symlink creation is unavailable")
    state = installs.home_path()
    state.mkdir()
    (state / "installs.json").write_text(json.dumps({"links": {str(link): str(source)}}), encoding="utf-8")
    assert _by_id(doctor.run_checks(network=False))["skill_installed"].ok
    source.rmdir()
    for options in ([], ["--public"]):
        main(["doctor", "--no-network", *options])
        checks = json.loads(capsys.readouterr().out)["checks"]
        assert next(check for check in checks if check["id"] == "skill_installed")["ok"] is False
