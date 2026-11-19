"""``install.sh`` run as ``curl ... | sh``: the downloaded source must not outlive the script."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="POSIX sh not available")


def _archive(path: Path) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name in ("ULTRON-main/install.py", "ULTRON-main/skill/ultron/SKILL.md"):
            data = b"# synthetic\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def _fake_tool(bin_dir: Path, name: str, body: str) -> None:
    tool = bin_dir / name
    tool.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
    tool.chmod(0o755)


@pytest.mark.parametrize("rc", [0, 3])
def test_piped_install_removes_downloaded_source_and_keeps_exit_code(tmp_path, rc):
    tmp_root = tmp_path / "tmp"
    bin_dir = tmp_path / "bin"
    work = tmp_path / "work"
    for folder in (tmp_root, bin_dir, work):
        folder.mkdir()
    archive = tmp_path / "ultron.tar.gz"
    _archive(archive)
    # curl copies the prepared archive to the -o target; uv proves it ran from the extracted source.
    _fake_tool(bin_dir, "curl", f'while [ "$#" -gt 1 ]; do [ "$1" = -o ] && cp "{archive.as_posix()}" "$2"; '
                                'shift; done')
    # The fake installer drains stdin, as a prompt would: the rest of a piped script must not be consumed.
    _fake_tool(bin_dir, "uv", f'cat >/dev/null; for a in "$@"; do case "$a" in *install.py) [ -f "$a" ] || exit 99;; esac; done; '
                              f'echo ran >&2; exit {rc}')
    env = {**os.environ, "HOME": str(tmp_path / "home"), "TMPDIR": tmp_root.as_posix(),
           "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])}
    script = (REPO / "install.sh").read_bytes()
    proc = subprocess.run([SH], input=script, cwd=work, env=env, capture_output=True, timeout=60, check=False)
    assert b"ran" in proc.stderr, proc.stderr
    assert proc.returncode == rc, proc.stderr
    assert list(tmp_root.iterdir()) == []  # the trap removed the mktemp folder


def test_sigterm_to_launcher_reaches_installer_and_keeps_its_stdin(tmp_path):
    src = tmp_path / "src"
    (src / "skill" / "ultron").mkdir(parents=True)
    (src / "install.py").write_text("# synthetic\n", encoding="utf-8")
    (src / "skill" / "ultron" / "SKILL.md").write_text("# synthetic\n", encoding="utf-8")
    (src / "install.sh").write_bytes((REPO / "install.sh").read_bytes())
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    o = out.as_posix()
    # The fake installer records its stdin, then runs ~10 s unless TERM stops it.
    _fake_tool(bin_dir, "uv", f'trap \'echo term > "{o}/term"; exit 143\' TERM\n'
                              f'read line || line=none; echo "$line" > "{o}/stdin"; touch "{o}/started"\n'
                              f'i=0; while [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done; touch "{o}/finished"')
    # The signal goes to the launcher PID only (not the process group), from inside sh so it also works with
    # an MSYS sh on Windows.
    driver = (
        'echo hello | sh ./install.sh & pid=$!\n'
        f'i=0; while [ ! -f "{o}/started" ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done\n'
        'kill -TERM "$pid"; wait "$pid"; echo "rc=$?"\n'
    )
    env = {**os.environ, "HOME": str(tmp_path / "home"),
           "PATH": os.pathsep.join([str(bin_dir), os.environ.get("PATH", "")])}
    proc = subprocess.run([SH, "-c", driver], cwd=src, env=env, capture_output=True, timeout=60, check=False)
    assert b"rc=143" in proc.stdout, (proc.stdout, proc.stderr)
    assert (out / "term").is_file()
    assert not (out / "finished").exists()
    assert (out / "stdin").read_text(encoding="utf-8").strip() == "hello"
