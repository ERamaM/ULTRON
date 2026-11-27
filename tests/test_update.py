"""``ultron update``: release lookup, archive verification, scope reconstruction and the JSON contract.

No test touches the network: the release API goes through a patched ``httpx.get`` and the archive download
through an ``httpx.MockTransport``.
"""

from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import httpx
import pytest

from ultron import cli
from ultron.infra import installs
from ultron.update import archive as update_archive
from ultron.update import copies as update_copies
from ultron.update import installer as update_installer
from ultron.update import notice as update_notice
from ultron.update import prepare as update

REPO = "https://github.com/ERamaM/ULTRON"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    home = tmp_path / "userhome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("ULTRON_REPO", raising=False)
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(installs, "in_dev_checkout", lambda: False)
    monkeypatch.setattr(update_archive, "_TRANSPORT", httpx.MockTransport(_no_network))
    monkeypatch.setattr(update_notice.httpx, "get", _no_api)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(cli, "__version__", "0.1.0")
    monkeypatch.setattr(update, "__version__", "0.1.0")
    return home


def _no_network(request):
    raise AssertionError(f"unexpected download {request.url}")


def _no_api(url, *, timeout, trust_env):
    raise AssertionError(f"unexpected API call {url}")


def _api(monkeypatch, releases: dict[str, dict] | Exception, latest: str | None = None):
    calls = []

    def get(url, *, timeout, trust_env):
        assert trust_env is False  # proxies from the environment are ignored, as in net.py
        calls.append(url)
        if isinstance(releases, Exception):
            raise releases
        request = httpx.Request("GET", url)
        if url.endswith("/releases/latest"):
            if latest is None:
                return httpx.Response(404, json={}, request=request)
            return httpx.Response(200, json=releases[latest], request=request)
        tag = url.rsplit("/", 1)[1]
        if tag in releases:
            return httpx.Response(200, json=releases[tag], request=request)
        return httpx.Response(404, json={}, request=request)

    monkeypatch.setattr(update_notice.httpx, "get", get)
    return calls


def _release(tag, **extra):
    return {"tag_name": tag, "html_url": f"{REPO}/releases/tag/{tag}", **extra}


def _zip(members: dict[str, bytes | str], *, symlink: str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
        if symlink:
            info = zipfile.ZipInfo(symlink)
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "../../outside")
    return buf.getvalue()


def _source_zip(version="0.2.0", top=None, extra=None, drop=()):
    top = top or f"ULTRON-{version}"
    members = {
        f"{top}/install.py": "print('installer')\n",
        f"{top}/skill/ultron/SKILL.md": "---\nname: ultron\n---\n",
        f"{top}/skill/ultron/scripts/ultron/__init__.py": f'__version__ = "{version}"\n',
    }
    for name in drop:
        members.pop(f"{top}/{name}")
    members.update(extra or {})
    return members


def _serve(monkeypatch, payload: bytes, tag="v0.2.0"):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        if request.url.host == "github.com":
            assert request.url.path == f"/ERamaM/ULTRON/archive/refs/tags/{tag}.zip"
            return httpx.Response(302, headers={"Location": f"https://codeload.github.com/ERamaM/ULTRON/zip/refs/tags/{tag}"})
        assert request.url.host == "codeload.github.com"
        return httpx.Response(200, content=payload)

    monkeypatch.setattr(update_archive, "_TRANSPORT", httpx.MockTransport(handler))
    return seen


def _install(path: Path, version: str | None = None, *, state: Path | None = None):
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text("---\nname: ultron\n---\n", encoding="utf-8")
    marker = {"name": "ultron", "installer": "install.py", "path": str(path)}
    if version:
        marker["version"] = version
    if state is not None:
        marker["ultron_home"] = str(state)
    (path / ".ultron-install.json").write_text(json.dumps(marker), encoding="utf-8")


def _record(records: list[dict]):
    """Write ``installs.json`` the way install.py does (``links`` plus ``installs`` keyed by path)."""
    home = Path(os.environ["ULTRON_HOME"])
    home.mkdir(parents=True, exist_ok=True)
    installs = {}
    for record in records:
        installs[record["path"]] = {"mode": "copy", "installed_at": "2026-01-01T00:00:00+00:00", "project": None,
                                    **record}
    (home / "installs.json").write_text(json.dumps({"links": {}, "installs": installs}), encoding="utf-8")


def _run(capsys, *argv):
    code = cli.main(["update", *argv])
    out = capsys.readouterr().out
    assert out.count("\n") == 1, out  # stdout stays a single JSON object
    return code, json.loads(out)


# ------------------------------------------------------------------------------------------------ --check


def test_check_reports_a_newer_release(monkeypatch, capsys):
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    code, out = _run(capsys, "--check")
    assert code == 0
    assert out == {
        "installed": "0.1.0",
        "latest": "0.2.0",
        "update_available": True,
        "release_url": f"{REPO}/releases/tag/v0.2.0",
    }


def test_check_same_version_has_no_update(monkeypatch, capsys):
    _api(monkeypatch, {"v0.1.0": _release("v0.1.0")}, latest="v0.1.0")
    code, out = _run(capsys, "--check")
    assert code == 0
    assert out["update_available"] is False and out["latest"] == "0.1.0"


@pytest.mark.parametrize("flag", ["prerelease", "draft"])
def test_check_ignores_prereleases_and_drafts(monkeypatch, capsys, flag):
    _api(monkeypatch, {"v0.3.0": _release("v0.3.0", **{flag: True})}, latest="v0.3.0")
    code, out = _run(capsys, "--check")
    assert code == 0
    assert out["update_available"] is False and out["latest"] is None


def test_check_offline_is_a_json_error_not_a_crash(monkeypatch, capsys):
    _api(monkeypatch, httpx.ConnectError("offline"))
    code, out = _run(capsys, "--check")
    assert code == 1
    assert out["error"] == "update_check_failed"


def test_check_never_downloads(monkeypatch, capsys):
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _run(capsys, "--check")  # _TRANSPORT raises on any download


# -------------------------------------------------------------------------------------------------- --to


@pytest.mark.parametrize("value", ["main", "1.2", "v1.2.3-rc1", "../v1.2.3", "v01.2.3", "latest"])
def test_to_rejects_non_semver(capsys, value):
    code, out = _run(capsys, "--to", value)
    assert code == 2 and out["error"] == "usage"


def test_check_and_to_are_exclusive(capsys):
    code, out = _run(capsys, "--check", "--to", "v1.2.3")
    assert code == 2 and out["error"] == "usage"


def test_to_accepts_vxyz_and_requires_a_stable_release(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, {"v0.3.0": _release("v0.3.0", prerelease=True)})
    code, out = _run(capsys, "--to", "v0.3.0")
    assert code == 2 and out["error"] == "release_not_found"
    code, out = _run(capsys, "--to", "v0.4.0")
    assert code == 2 and out["error"] == "release_not_found"


def test_to_accepts_a_version_without_v_prefix(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")})
    _serve(monkeypatch, _zip(_source_zip("0.2.0")))
    code, out = _run(capsys, "--to", "0.2.0")
    assert code == 0 and out["tag"] == "v0.2.0"


def test_to_resolves_a_release_tagged_without_v(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    calls = _api(monkeypatch, {"0.2.0": _release("0.2.0")})
    _serve(monkeypatch, _zip(_source_zip("0.2.0")), tag="0.2.0")
    code, out = _run(capsys, "--to", "0.2.0")
    assert code == 0 and out["tag"] == "0.2.0"
    assert [c.rsplit("/", 1)[1] for c in calls] == ["v0.2.0", "0.2.0"]


# --------------------------------------------------------------------------------------------- prepare


def test_update_prepares_command_and_never_runs_it(monkeypatch, capsys, tmp_path):
    home = Path.home()
    _install(home / ".claude" / "skills" / "ultron")
    _install(home / ".config" / "opencode" / "skills" / "ultron")
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    seen = _serve(monkeypatch, _zip(_source_zip("0.2.0")))
    ran = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(update_installer, "find_on_path", lambda name: None)

    code, out = _run(capsys)
    assert code == 0 and not ran
    assert seen[0].startswith("https://github.com/") and seen[1].startswith("https://codeload.github.com/")
    assert out["status"] == "ready"
    assert (out["installed"], out["target"], out["tag"]) == ("0.1.0", "0.2.0", "v0.2.0")
    source = Path(out["source"])
    assert (source / "install.py").is_file() and (source / "skill" / "ultron" / "SKILL.md").is_file()
    assert Path(out["cleanup"]) in source.parents
    [command] = out["commands"]
    argv = command["argv"]
    assert argv[argv.index(str(source / "install.py")) + 1:] == ["--update", "--global", "--platforms", "claude,opencode"]
    assert command["scope"] == "global"
    assert isinstance(command["display"], str) and "--update" in command["display"]
    assert any("ultron doctor" in step for step in out["next_steps"])
    assert any("restart" in step.lower() for step in out["next_steps"])
    assert out["env"] == {"ULTRON_HOME": str(Path(os.environ["ULTRON_HOME"]))}


def test_without_uv_the_base_interpreter_runs_the_installer(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _serve(monkeypatch, _zip(_source_zip("0.2.0")))
    monkeypatch.setattr(update_installer, "find_on_path", lambda name: None)
    monkeypatch.setattr(sys, "_base_executable", str(Path.home() / "base python" / "python3"), raising=False)
    _, out = _run(capsys)
    assert out["commands"][0]["argv"][0] == str(Path.home() / "base python" / "python3")


def test_release_tag_without_v_prefix_is_kept(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, {"0.2.0": _release("0.2.0")}, latest="0.2.0")
    _serve(monkeypatch, _zip(_source_zip("0.2.0")), tag="0.2.0")
    code, out = _run(capsys)
    assert code == 0 and out["tag"] == "0.2.0"
    assert out["release_url"].endswith("/releases/tag/0.2.0")


def test_installer_runs_outside_the_ultron_venv(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _serve(monkeypatch, _zip(_source_zip("0.2.0")))
    monkeypatch.setattr(update_installer, "find_on_path", lambda name: "/opt/tools/uv" if name == "uv" else None)
    _, out = _run(capsys)
    argv = out["commands"][0]["argv"]
    assert argv[:5] == ["/opt/tools/uv", "run", "--python", ">=3.12", "--no-project"]
    # --isolated: an active VIRTUAL_ENV (ULTRON's own venv) must not become the installer's interpreter
    assert "--isolated" in argv[:argv.index(out["source"] + os.sep + "install.py")]
    assert any("VIRTUAL_ENV" in step for step in out["next_steps"])


def test_mixed_antigravity_copy_and_dev_link_is_left_for_manual_handling(monkeypatch, capsys):
    home = Path.home()
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    _install(home / ".claude" / "skills" / "ultron")
    _install(home / ".gemini" / "config" / "skills" / "ultron")
    legacy = home / ".gemini" / "antigravity" / "skills" / "ultron"
    monkeypatch.setattr(update_copies, "_is_link", lambda path: Path(path) == legacy)
    monkeypatch.setattr(installs, "registered_link", lambda path: Path(path) == legacy)
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _serve(monkeypatch, _zip(_source_zip("0.2.0")))
    code, out = _run(capsys)
    assert code == 0
    [command] = out["commands"]
    assert command["argv"][-2:] == ["--platforms", "claude"]  # antigravity would also replace the dev link
    assert out["manual"][0]["platform"] == "antigravity"
    assert str(legacy) in out["manual"][0]["dev_links"]


def test_mixed_antigravity_only_gives_no_command(monkeypatch, capsys):
    home = Path.home()
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    _install(home / ".gemini" / "config" / "skills" / "ultron")
    legacy = home / ".gemini" / "antigravity" / "skills" / "ultron"
    monkeypatch.setattr(update_copies, "_is_link", lambda path: Path(path) == legacy)
    monkeypatch.setattr(installs, "registered_link", lambda path: Path(path) == legacy)
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    code, out = _run(capsys)
    assert code == 0 and out["status"] == "no_installs" and out["commands"] == []
    assert out["manual"][0]["platform"] == "antigravity"


def test_missing_records_keeps_inaccessible_paths(monkeypatch, tmp_path):
    blocked = tmp_path / "blocked" / ".claude" / "skills" / "ultron"
    gone = tmp_path / "gone" / ".claude" / "skills" / "ultron"
    _record([{"scope": "project", "project": str(p.parents[2]), "path": str(p), "platforms": ["claude"],
              "version": "0.1.0"} for p in (blocked, gone)])
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        if Path(path) == blocked:
            raise PermissionError("denied")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", lstat)
    monkeypatch.setattr(Path, "exists", lambda self, **kw: False)  # Python 3.14: errors read as "missing"
    assert update_copies.missing_records() == [str(gone)]


def test_up_to_date_returns_no_command(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, {"v0.1.0": _release("v0.1.0")}, latest="v0.1.0")
    code, out = _run(capsys)
    assert code == 0
    assert out["status"] == "up_to_date" and out["commands"] == [] and "source" not in out


def test_no_install_found_returns_no_command(monkeypatch, capsys):
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    code, out = _run(capsys)
    assert code == 0
    assert out["status"] == "no_installs" and out["commands"] == []


def test_offline_update_is_a_json_error(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, httpx.ConnectError("offline"))
    code, out = _run(capsys)
    assert code == 1 and out["error"] == "update_unavailable"


# ------------------------------------------------------------------------------------------------- scopes


def test_scopes_from_global_and_project_markers(monkeypatch, capsys, tmp_path):
    home = Path.home()
    _install(home / ".agents" / "skills" / "ultron")
    project = tmp_path / "mi proyecto ñ"
    _install(project / ".claude" / "skills" / "ultron")
    other = Path.cwd()
    _install(other / ".agents" / "skills" / "ultron")
    (home / ".gemini" / "config" / "skills" / "ultron").mkdir(parents=True)  # foreign: no marker

    installs = update_copies.discover_installs(home=home, cwd=other, projects=[project])
    scopes = {(i["scope"], i.get("project")): i["platforms"] for i in installs}
    assert scopes == {
        ("global", None): ["codex"],
        ("project", str(other)): ["codex", "opencode", "antigravity"],
        ("project", str(project)): ["claude"],
    }

    commands = update_installer.installer_commands(Path("src dir") / "install.py", installs)
    tails = sorted(c["argv"][c["argv"].index(str(Path("src dir") / "install.py")) + 1:] for c in commands)
    assert ["--update", "--global", "--platforms", "codex"] in tails
    assert ["--update", "--project", str(project), "--platforms", "claude"] in tails
    assert ["--update", "--project", str(other), "--platforms", "codex,opencode,antigravity"] in tails


def test_recorded_project_install_outside_cwd_is_found(monkeypatch, capsys, tmp_path):
    project = (tmp_path / "elsewhere ñ").resolve()
    agents = project / ".agents" / "skills" / "ultron"
    _install(agents)
    _record([{"scope": "project", "project": str(project), "path": str(agents),
              "platforms": ["codex", "opencode"], "version": "0.1.0"}])
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _serve(monkeypatch, _zip(_source_zip("0.2.0")))
    code, out = _run(capsys)  # no --project: the record finds it
    assert code == 0 and out["status"] == "ready" and out["reason"] == "cli_outdated"
    [command] = out["commands"]
    assert command["argv"][-4:] == ["--project", str(project), "--platforms", "codex,opencode"]
    assert out["installs"][0]["versions"] == {str(agents): "0.1.0"}


def test_stale_project_copy_gets_a_command_when_cli_is_current(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(update, "__version__", "0.2.0")
    home = Path.home()
    global_copy = home / ".claude" / "skills" / "ultron"
    _install(global_copy)
    project = (tmp_path / "old project").resolve()
    stale = project / ".claude" / "skills" / "ultron"
    _install(stale)
    _record([
        {"scope": "global", "path": str(global_copy), "platforms": ["claude"], "version": "0.2.0"},
        {"scope": "project", "project": str(project), "path": str(stale), "platforms": ["claude"], "version": "0.1.0"},
    ])
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _serve(monkeypatch, _zip(_source_zip("0.2.0")))
    code, out = _run(capsys)
    assert code == 0 and out["status"] == "ready" and out["reason"] == "stale_skill_copies"
    [command] = out["commands"]
    assert command["scope"] == "project" and command["argv"][-4:] == ["--project", str(project), "--platforms", "claude"]


def test_current_recorded_copies_are_up_to_date(monkeypatch, capsys):
    monkeypatch.setattr(update, "__version__", "0.2.0")
    global_copy = Path.home() / ".claude" / "skills" / "ultron"
    _install(global_copy)
    _record([{"scope": "global", "path": str(global_copy), "platforms": ["claude"], "version": "0.2.0"}])
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    code, out = _run(capsys)
    assert code == 0 and out["status"] == "up_to_date" and out["commands"] == []


def test_cli_ahead_of_latest_release_never_moves_back(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(update, "__version__", "0.3.0")
    project = (tmp_path / "p").resolve()
    stale = project / ".claude" / "skills" / "ultron"
    _install(stale, "0.1.0")
    _record([{"scope": "project", "project": str(project), "path": str(stale), "platforms": ["claude"],
              "version": "0.1.0"}])
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    code, out = _run(capsys)
    assert code == 0 and out["status"] == "up_to_date" and out["commands"] == []


def test_marker_version_wins_over_a_lagging_record(monkeypatch, capsys):
    monkeypatch.setattr(update, "__version__", "0.2.0")
    copy = Path.home() / ".claude" / "skills" / "ultron"
    _install(copy, "0.2.0")  # recopied, but the record write failed
    _record([{"scope": "global", "path": str(copy), "platforms": ["claude"], "version": "0.1.0"}])
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    code, out = _run(capsys)
    assert code == 0 and out["status"] == "up_to_date"


def test_records_written_by_the_real_installer_are_read(monkeypatch, tmp_path):
    import importlib.util

    spec = importlib.util.spec_from_file_location("ultron_install_for_update", Path(__file__).parents[1] / "install.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    monkeypatch.setenv("ULTRON_INSTALL_SKIP_VENV", "1")
    monkeypatch.setattr(installer, "_skill_version", lambda source=None: "0.1.0")
    home = Path.home()
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    project = (tmp_path / "proyecto con espacios ñ").resolve()
    project.mkdir()
    with pytest.MonkeyPatch.context() as quiet:
        quiet.setattr(installer, "print", lambda *a, **k: None, raising=False)
        assert installer.main(["--global", "--platforms", "claude,antigravity"]) == 0
        assert installer.main(["--project", str(project), "--platforms", "codex"]) == 0
    copies = update_copies.find_copies(home=home, cwd=tmp_path, projects=[])
    assert {c["found_by"] for c in copies} == {"record"}
    groups = {(g["scope"], g.get("project")): g["platforms"] for g in update_copies.group_scopes(copies)}
    assert groups == {("global", None): ["claude", "antigravity"], ("project", str(project)): ["codex"]}
    assert {c["version"] for c in copies} == {"0.1.0"}


def test_missing_recorded_install_falls_back_to_markers(monkeypatch, capsys, tmp_path):
    home = Path.home()
    _install(home / ".config" / "opencode" / "skills" / "ultron")  # pre-record install: marker only
    gone = tmp_path / "deleted project" / ".claude" / "skills" / "ultron"
    _record([{"scope": "project", "project": str(gone.parents[2]), "path": str(gone), "platforms": ["claude"],
              "version": "0.1.0"}])
    installs = update_copies.discover_installs(home=home, cwd=tmp_path, projects=[])
    assert [(i["scope"], i["platforms"]) for i in installs] == [("global", ["opencode"])]


def test_records_of_deleted_folders_are_reported_as_pruned(monkeypatch, capsys, tmp_path):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    gone = tmp_path / "deleted project" / ".claude" / "skills" / "ultron"
    _record([{"scope": "project", "project": str(gone.parents[2]), "path": str(gone), "platforms": ["claude"],
              "version": "0.1.0"}])
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _serve(monkeypatch, _zip(_source_zip("0.2.0")))
    code, out = _run(capsys)
    assert code == 0 and out["pruned_records"] == [str(gone)]
    assert [c["scope"] for c in out["commands"]] == ["global"]


def test_old_format_registry_falls_back_to_markers(tmp_path):
    home = Path.home()
    _install(home / ".claude" / "skills" / "ultron")
    ultron_home = Path(os.environ["ULTRON_HOME"])
    ultron_home.mkdir(parents=True, exist_ok=True)
    (ultron_home / "installs.json").write_text(json.dumps({"links": {}}), encoding="utf-8")
    [install] = update_copies.discover_installs(home=home, cwd=tmp_path, projects=[])
    assert install["scope"] == "global" and install["platforms"] == ["claude"]


def test_record_and_marker_for_the_same_folder_count_once(tmp_path):
    home = Path.home()
    copy = home / ".claude" / "skills" / "ultron"
    _install(copy)
    _record([{"scope": "global", "path": str(copy), "platforms": ["claude"], "version": "0.1.0"}])
    copies = update_copies.find_copies(home=home, cwd=tmp_path, projects=[])
    assert [(c["path"], c["found_by"]) for c in copies] == [(str(copy), "record")]


def test_project_option_is_repeatable(monkeypatch, capsys, tmp_path):
    project = tmp_path / "proj a"
    _install(project / ".claude" / "skills" / "ultron")
    _install(project / ".agents" / "skills" / "ultron")
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _serve(monkeypatch, _zip(_source_zip("0.2.0")))
    code, out = _run(capsys, "--project", str(project), "--project", str(project))
    assert code == 0
    [command] = out["commands"]
    assert command["argv"][-4:] == ["--project", str(project.resolve()), "--platforms",
                                    "claude,codex,opencode,antigravity"]


def test_legacy_antigravity_maps_to_antigravity(tmp_path):
    home = Path.home()
    (home / ".gemini" / "antigravity").mkdir(parents=True)
    _install(home / ".gemini" / "antigravity" / "skills" / "ultron")
    [install] = update_copies.discover_installs(home=home, cwd=tmp_path, projects=[])
    assert install["platforms"] == ["antigravity"]


def test_registered_dev_links_are_reported_but_not_updated(tmp_path, monkeypatch):
    home = Path.home()
    source = tmp_path / "checkout" / "skill" / "ultron"
    source.mkdir(parents=True)
    link = home / ".claude" / "skills" / "ultron"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available")
    ultron_home = Path(os.environ["ULTRON_HOME"])
    ultron_home.mkdir(parents=True, exist_ok=True)
    links = {str(link.parent.resolve() / link.name): str(source.resolve())}
    (ultron_home / "installs.json").write_text(json.dumps({"links": links}), encoding="utf-8")
    installs = update_copies.discover_installs(home=home, cwd=tmp_path, projects=[])
    assert installs == []
    assert update_copies.dev_links(home=home, cwd=tmp_path, projects=[]) == [str(link)]
    (ultron_home / "installs.json").write_text(json.dumps({"links": {}}), encoding="utf-8")
    assert update_copies.dev_links(home=home, cwd=tmp_path, projects=[]) == []  # unregistered: not ours


def test_source_checkout_returns_no_command(monkeypatch, capsys):
    monkeypatch.setattr(installs, "in_dev_checkout", lambda: True)
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    code, out = _run(capsys)
    assert code == 0 and out["status"] == "source_checkout" and out["commands"] == []


# ------------------------------------------------------------------------------------- archive safety


def _extract(tmp_path, members, **kw):
    archive = tmp_path / "a.zip"
    archive.write_bytes(_zip(members, **kw))
    dest = tmp_path / "out"
    dest.mkdir()
    return update_archive.extract_source(archive, dest, expected_version="0.2.0")


def test_archive_without_install_py_is_rejected(tmp_path):
    with pytest.raises(update_archive.ArchiveError, match="install.py"):
        _extract(tmp_path, _source_zip(drop=["install.py"]))


def test_archive_without_skill_md_is_rejected(tmp_path):
    with pytest.raises(update_archive.ArchiveError, match="SKILL.md"):
        _extract(tmp_path, _source_zip(drop=["skill/ultron/SKILL.md"]))


@pytest.mark.parametrize("name", [
    "ULTRON-0.2.0/../../evil.py",
    "../evil.py",
    "/etc/evil.py",
    "C:/evil.py",
    "ULTRON-0.2.0\\..\\..\\evil.py",
])
def test_traversal_members_are_rejected(tmp_path, name):
    with pytest.raises(update_archive.ArchiveError):
        _extract(tmp_path, _source_zip(extra={name: "x"}))
    assert not (tmp_path / "evil.py").exists()


def test_symlink_members_are_rejected(tmp_path):
    with pytest.raises(update_archive.ArchiveError, match="link"):
        _extract(tmp_path, _source_zip(), symlink="ULTRON-0.2.0/skill/ultron/link")


def test_size_limits_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(update_archive, "MAX_EXTRACTED_BYTES", 100)
    with pytest.raises(update_archive.ArchiveError, match="large"):
        _extract(tmp_path, _source_zip(extra={"ULTRON-0.2.0/big.bin": b"0" * 1000}))


def test_corrupt_member_is_a_typed_error(tmp_path):
    archive = tmp_path / "a.zip"
    data = bytearray(_zip(_source_zip()))
    marker = b"print('installer')"
    start = data.index(marker)
    data[start:start + len(marker)] = b"X" * len(marker)  # stored data no longer matches its CRC
    archive.write_bytes(bytes(data))
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(update_archive.ArchiveError, match="corrupt"):
        update_archive.extract_source(archive, dest, expected_version="0.2.0")


def test_version_mismatch_is_rejected(tmp_path):
    with pytest.raises(update_archive.ArchiveError, match="version"):
        _extract(tmp_path, _source_zip(version="0.9.9", top="ULTRON-0.2.0"))


def test_bad_archive_returns_typed_error_and_cleans_up(monkeypatch, capsys, tmp_path):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _serve(monkeypatch, _zip(_source_zip(drop=["install.py"])))
    code, out = _run(capsys)
    assert code == 1 and out["error"] == "archive_invalid"
    assert list((tmp_path / "tmp").iterdir()) == []


def test_download_refuses_redirects_off_github(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")

    def handler(request):
        return httpx.Response(302, headers={"Location": "https://evil.example/x.zip"})

    monkeypatch.setattr(update_archive, "_TRANSPORT", httpx.MockTransport(handler))
    code, out = _run(capsys)
    assert code == 1 and out["error"] == "update_unavailable"


def test_download_size_cap(monkeypatch, capsys):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _serve(monkeypatch, b"0" * 5000)
    monkeypatch.setattr(update_archive, "MAX_DOWNLOAD_BYTES", 1000)
    code, out = _run(capsys)
    assert code == 1 and out["error"] == "update_unavailable"


@pytest.mark.parametrize("value", ["http://github.com/ERamaM/ULTRON", "https://evil.example/ERamaM/ULTRON",
                                   "https://github.com.evil.example/a/b", "https://github.com/a"])
def test_repository_override_must_be_github_https(monkeypatch, capsys, value):
    monkeypatch.setenv("ULTRON_REPO", value)
    code, out = _run(capsys, "--check")
    assert code == 2 and out["error"] == "invalid_repository"


def test_repository_override_is_honoured(monkeypatch, capsys):
    monkeypatch.setenv("ULTRON_REPO", "https://github.com/someone/ULTRON-fork/")
    calls = _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    _run(capsys, "--check")
    assert calls == ["https://api.github.com/repos/someone/ULTRON-fork/releases/latest"]


@pytest.mark.parametrize("other_platform", ["opencode", "antigravity-legacy"])
def test_update_commands_never_refresh_a_copy_owned_by_another_home(
    _env, tmp_path, monkeypatch, capsys, other_platform,
):
    ours, theirs = tmp_path / "state-a", tmp_path / "state-b"
    monkeypatch.setenv("ULTRON_HOME", str(ours))
    (_env / ".gemini" / "antigravity").mkdir(parents=True)
    paths = update_copies.global_skill_dirs(_env)
    _install(paths["claude"], state=ours)
    _install(paths[other_platform], state=theirs)
    if other_platform == "antigravity-legacy":
        _install(paths["antigravity"], state=ours)
    _serve(monkeypatch, _zip(_source_zip()))
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    code, out = _run(capsys)
    assert code == 0 and out["status"] == "ready"
    [command] = out["commands"]
    assert command["argv"][-2:] == ["--platforms", "claude"]
    assert out["env"] == {"ULTRON_HOME": str(ours)}
    assert any(str(paths[other_platform]) in item["paths"] for item in out["manual"])


@pytest.mark.parametrize("relative", [False, True])
def test_prepared_installers_keep_absolute_home_when_agent_changes_directory(tmp_path, monkeypatch, relative):
    _install(Path.home() / ".claude" / "skills" / "ultron")
    invocation_dir = Path.cwd()
    if relative:
        monkeypatch.setenv("ULTRON_HOME", "relative-state")
        expected = invocation_dir / "relative-state"
    else:
        monkeypatch.delenv("ULTRON_HOME")
        expected = Path.home() / ".ultron"
    _serve(monkeypatch, _zip(_source_zip()))
    _api(monkeypatch, {"v0.2.0": _release("v0.2.0")}, latest="v0.2.0")
    result = update.prepare_update()
    monkeypatch.chdir(tmp_path)
    assert result["env"] == {"ULTRON_HOME": str(expected)}
    assert str(expected) in result["commands"][0]["display"]
