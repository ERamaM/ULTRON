"""The ``install.py --update`` command lines: locating ``uv`` or a base interpreter and building argv and env."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from ultron.infra.home import home_path

# ------------------------------------------------------------------------------------------- commands


def find_on_path(name: str) -> str | None:
    """``name`` on PATH, never from the current directory (unlike ``shutil.which`` on Windows).

    Only absolute PATH entries are searched; empty, relative and current-directory entries are skipped, so an
    untrusted checkout holding ``uv.exe`` cannot become the installer's executable. On Windows the ``PATHEXT``
    extensions are tried. The same resolver lives in ``install.py`` and ``ultron.update.prepare`` (tested together).
    """
    try:
        cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    except OSError:
        cwd = None
    exts = [""]
    if os.name == "nt":
        exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e.strip()]
        if os.path.splitext(name)[1]:
            exts.insert(0, "")
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        try:
            if os.path.normcase(os.path.realpath(entry)) == cwd:
                continue
        except OSError:
            continue
        for ext in exts:
            candidate = os.path.join(entry, name + ext)
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _base_interpreter() -> str:
    """The interpreter ULTRON's venv was created from (never a path relative to the current directory)."""
    base = getattr(sys, "_base_executable", None)
    if base and os.path.isabs(base):
        return base
    name = "python.exe" if os.name == "nt" else os.path.join("bin", "python3")
    candidate = os.path.join(sys.base_prefix, name)
    return candidate if os.path.isfile(candidate) else sys.executable


def _runner(install_py: Path) -> list[str]:
    """Run ``install.py`` outside ULTRON's venv (the installer rebuilds it), like the bootstrap scripts do."""
    uv = find_on_path("uv")
    if uv:
        # --isolated: never run inside an active venv (VIRTUAL_ENV may be ULTRON's own, which the installer rebuilds)
        return [uv, "run", "--python", ">=3.12", "--no-project", "--isolated", str(install_py)]
    return [_base_interpreter(), str(install_py)]


def _display(argv: list[str], env: dict[str, str]) -> str:
    """The command as one line for the current shell: PowerShell on Windows, POSIX sh elsewhere."""
    if os.name == "nt":
        quote = "'{}'".format
        prefix = "".join(f"$env:{k} = {quote(v.replace(chr(39), chr(39) * 2))}; " for k, v in env.items())
        return prefix + "& " + " ".join(quote(a.replace("'", "''")) for a in argv)
    prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
    return prefix + shlex.join(argv)


def installer_env() -> dict[str, str]:
    """Build environment variables preserved when generating update commands."""
    return {"ULTRON_HOME": str(home_path())}


def installer_commands(install_py: Path, installs: list[dict], *, env: dict[str, str] | None = None) -> list[dict]:
    """One ``install.py --update`` per scope (``--global`` and ``--project`` are mutually exclusive)."""
    env = installer_env() if env is None else env
    commands = []
    for install in installs:
        scope_args = ["--global"] if install["scope"] == "global" else ["--project", install["project"]]
        argv = [*_runner(install_py), "--update", *scope_args, "--platforms", ",".join(install["platforms"])]
        command = {"scope": install["scope"], "argv": argv, "display": _display(argv, env)}
        if install["scope"] == "project":
            command["project"] = install["project"]
        commands.append(command)
    return commands


