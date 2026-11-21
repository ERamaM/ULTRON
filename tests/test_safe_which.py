"""``find_on_path``: the uv lookup in install.py and ``ultron update`` never picks a binary from the cwd.

``shutil.which`` on Windows searches the current directory before PATH, so an untrusted checkout holding a
``uv.exe`` would become the installer's argv[0]. Both copies of the resolver are tested the same way.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from ultron.update import installer as update_installer

REPO = Path(__file__).resolve().parents[1]
EXE = "uv.exe" if os.name == "nt" else "uv"


def _installer():
    spec = importlib.util.spec_from_file_location("ultron_install_which", REPO / "install.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def _windows_default_search(monkeypatch):
    # Some shells set this to stop Windows searching the cwd; the user's agent may not.
    monkeypatch.delenv("NoDefaultCurrentDirectoryInExePath", raising=False)


@pytest.fixture(params=["install.py", "ultron.update.prepare"])
def find(request):
    return _installer().find_on_path if request.param == "install.py" else update_installer.find_on_path


def _fake_uv(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / EXE
    path.write_bytes(b"#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def test_uv_in_the_cwd_is_never_chosen(find, tmp_path, monkeypatch):
    work = tmp_path / "untrusted checkout"
    _fake_uv(work)
    monkeypatch.chdir(work)
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", os.pathsep.join([str(empty), "", ".", str(work)]))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    assert find("uv") is None


def test_uv_on_an_absolute_path_entry_is_chosen(find, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tools = tmp_path / "tools dir ñ"
    uv = _fake_uv(tools)
    monkeypatch.setenv("PATH", os.pathsep.join([str(tmp_path / "missing"), str(tools)]))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    assert Path(find("uv")) == uv


def test_relative_path_entries_are_ignored(find, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _fake_uv(tmp_path / "bin")
    monkeypatch.setenv("PATH", os.pathsep.join(["bin", os.path.join(".", "bin")]))
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    assert find("uv") is None


def test_the_cwd_without_uv_falls_back_to_the_base_interpreter(tmp_path, monkeypatch):
    work = tmp_path / "w"
    _fake_uv(work)
    monkeypatch.chdir(work)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    monkeypatch.setattr(sys, "_base_executable", str(tmp_path / "base" / "python"), raising=False)
    argv = update_installer._runner(Path("src") / "install.py")
    assert argv[0] == str(tmp_path / "base" / "python")


def test_both_copies_are_identical():
    import inspect

    assert inspect.getsource(_installer().find_on_path) == inspect.getsource(update_installer.find_on_path)
