"""ULTRON installer (standard library only).

    python install.py [--global | --project DIR] [--platforms claude,codex,opencode,antigravity]
                      [--dev] [--update] [--uninstall [--purge-data]] [--dry-run]
    python install.py --dev [--target DIR] [--copy]          # phase-1 developer install (unchanged)

1. Checks Python >= 3.12.
2. Creates/updates ``$ULTRON_HOME/venv`` (default ``~/.ultron/venv``) with ``uv venv`` + ``uv pip`` when ``uv``
   is on PATH, else ``python -m venv`` + ``pip``, installing ``skill/ultron/scripts[cv,xlsx]`` (editable only
   with ``--dev``). ``ULTRON_INSTALL_SKIP_VENV=1`` skips this step (tests).
3. Copies ``skill/ultron`` to every target (full copy; ``--dev`` tries a symlink first) and writes ``RUNTIME.md``
   (absolute path of the ``ultron`` executable, version, date) and the marker ``.ultron-install.json``.
4. Prints one JSON summary with a status per target (``installed`` / ``updated`` / ``skipped`` + reason).

Scope: ``--global`` (default unless ``--dev``/``--project``) installs into the platforms' user folders; default
platforms are those whose base folder exists (all four if none does). ``--project DIR`` installs into
``DIR/.claude/skills/ultron`` and ``DIR/.agents/skills/ultron``. ``--dev`` without a scope keeps the phase-1
behaviour: project install into ``--target`` (default ``.``).

``--update`` refreshes the venv and only the targets that already hold an ULTRON install (others: skipped).
``--uninstall`` removes only folders that contain ``.ultron-install.json``; ``$ULTRON_HOME`` is kept unless
``--purge-data`` is given, and even then only if it holds ``.ultron-home`` (written by every install) or
``ultron.sqlite3`` (with only the database, generic entries such as ``venv`` or ``cv`` are kept). Nothing outside the ``ultron`` folders this installer created is ever deleted: destinations
inside the source tree are refused, a marker only counts when its ``path`` is the folder itself, ``--dev`` links
are tracked in ``$ULTRON_HOME/installs.json`` (no marker is written through them), unmarked phase-1 folders are
moved to ``$ULTRON_HOME/backups/legacy-skill-<platform>-<timestamp>``, and links are unlinked, never followed.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent
SKILL_SOURCE = REPO / "skill" / "ultron"
PLATFORM_DIRS = (Path(".claude") / "skills", Path(".agents") / "skills")
MARKER = ".ultron-install.json"
PLATFORMS = ("claude", "codex", "opencode", "antigravity")
EXTRAS = "[cv,xlsx]"
COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__", "*.pyc", "*.egg-info", ".venv", ".pytest_cache", "RUNTIME.md", MARKER
)
PYTHON_HINT = (
    "Install Python 3.12 or newer (https://www.python.org/downloads/, `winget install Python.Python.3.12`, "
    "`brew install python@3.12`, your Linux package manager, or `uv python install 3.12`) and re-run "
    "install.py with it."
)


class InstallError(RuntimeError):
    pass


class UsageError(Exception):
    pass


# ----------------------------------------------------------------------------------------------- paths


def ultron_home() -> Path:
    env = os.environ.get("ULTRON_HOME")
    return Path(env).expanduser() if env else Path.home() / ".ultron"


def ultron_executable(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Scripts" / "ultron.exe"
    return venv / "bin" / "ultron"


def _venv_python(venv: Path) -> Path:
    return venv / "Scripts" / "python.exe" if os.name == "nt" else venv / "bin" / "python"


def detect_platforms() -> list[str]:
    """Platforms whose base folder exists in the user's home; all four when none does."""
    home = Path.home()
    present = {
        "claude": (home / ".claude").is_dir(),
        "codex": (home / ".agents").is_dir() or (home / ".codex").is_dir(),
        "opencode": (home / ".config" / "opencode").is_dir(),
        "antigravity": (home / ".gemini").is_dir(),
    }
    found = [name for name in PLATFORMS if present[name]]
    return found or list(PLATFORMS)


def platform_targets(scope: str, project: Path | None, platforms: list[str]) -> dict[str, Path]:
    """Skill folders for ``platforms``; keys are platform names (plus ``antigravity-legacy``)."""
    targets: dict[str, Path] = {}
    if scope == "project":
        if project is None:
            raise ValueError("project scope needs a folder")
        for name in platforms:
            base = ".claude" if name == "claude" else ".agents"
            targets[name] = Path(project) / base / "skills" / "ultron"
        return targets
    home = Path.home()
    for name in platforms:
        if name == "claude":
            targets[name] = home / ".claude" / "skills" / "ultron"
        elif name == "codex":
            targets[name] = home / ".agents" / "skills" / "ultron"
        elif name == "opencode":
            targets[name] = home / ".config" / "opencode" / "skills" / "ultron"
        elif name == "antigravity":
            targets[name] = home / ".gemini" / "config" / "skills" / "ultron"
            if (home / ".gemini" / "antigravity").is_dir():
                targets["antigravity-legacy"] = home / ".gemini" / "antigravity" / "skills" / "ultron"
    return targets


def _group_by_path(targets: dict[str, Path]) -> list[tuple[Path, list[str]]]:
    """Several platforms can share one folder (e.g. ``.agents`` in a project): install it once."""
    groups: dict[Path, list[str]] = {}
    for name, path in targets.items():
        groups.setdefault(path, []).append(name)
    return list(groups.items())


# ------------------------------------------------------------------------------------------------ venv


# Termination requested by SIGTERM/SIGHUP: its exit code, and whether a pip/venv step is running right now.
_TERMINATION = {"code": 0, "in_step": False}


class Terminated(BaseException):
    """SIGTERM/SIGHUP stopped the installer; ``main`` reports it as one JSON object with exit code ``code``."""

    def __init__(self, code: int) -> None:
        super().__init__(f"stopped by signal (exit {code})")
        self.code = code


def _exit_if_terminated() -> None:
    """Checkpoint between steps and file replacements: stop here if a termination signal was received."""
    if _TERMINATION["code"]:
        raise Terminated(_TERMINATION["code"])


_STEP_STOP_GRACE = 10.0  # seconds a step's process group gets to exit after SIGTERM before SIGKILL


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_step(proc: subprocess.Popen) -> None:
    """Stop a step and every process it started (build backends included), then reap it."""
    if os.name != "posix":
        proc.kill()
        proc.wait()
        return
    for sig, grace in ((signal.SIGTERM, _STEP_STOP_GRACE), (signal.SIGKILL, 5.0)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            if proc.poll() is not None and not _group_alive(proc.pid):
                break
            time.sleep(0.05)
        else:
            continue
        break
    proc.wait()


def _run(cmd: list[str]) -> None:
    """Run an installer step; its output goes to stderr so stdout stays one JSON object.

    On POSIX the step gets its own process group, so an interruption can stop pip/uv and the build backends
    they start before ``install.sh`` removes a downloaded source those processes still read.
    """
    _exit_if_terminated()  # a pending termination stops before the next step, never inside file moves
    # A signal while the step is being started is only recorded (in_step is still False), so the Popen object
    # is never lost; it is acted on right after Popen returns.
    proc = subprocess.Popen(cmd, stdout=sys.stderr, start_new_session=os.name == "posix")
    _TERMINATION["in_step"] = True
    try:
        _exit_if_terminated()
        code = proc.wait()
    except BaseException:
        _TERMINATION["in_step"] = False  # a second signal during cleanup is recorded, not raised
        _stop_step(proc)
        raise
    finally:
        _TERMINATION["in_step"] = False
    if code:
        raise subprocess.CalledProcessError(code, cmd)


def _on_termination_signal(signum: int, _frame: object) -> None:
    _TERMINATION["code"] = 128 + signum
    if _TERMINATION["in_step"]:
        raise Terminated(128 + signum)  # _run stops and reaps the step's process group, then re-raises


def _handle_termination_signals() -> None:
    """Stop cleanly on SIGTERM/SIGHUP instead of dying with a pip/venv step still running.

    With the default handlers only this process dies, and ``install.sh`` would then delete a downloaded source
    that the orphaned step still uses. A signal during a step ends it now; at any other moment (for example
    while a previous install is moved aside and replaced) it takes effect at the next checkpoint: before the
    next step, the next target, a data purge, or the final report. File replacement is never cut in half.
    """
    for name in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _on_termination_signal)


def create_venv(venv: Path, scripts: Path, *, editable: bool = True) -> str:
    """Create/update the venv and install ``scripts[cv,xlsx]`` (editable or regular); return the tool used."""
    spec = f"{scripts}{EXTRAS}"
    uv = shutil.which("uv")
    if uv:
        _run([uv, "venv", "--allow-existing", "--python", ">=3.12", str(venv)])
        if editable:
            _run([uv, "pip", "install", "--python", str(_venv_python(venv)), "-e", spec])
        else:  # uv reuses cached local builds at the same version unless told to rebuild
            _run([uv, "pip", "install", "--python", str(_venv_python(venv)), "--reinstall-package", "ultron", spec])
        return "uv"
    _run([sys.executable, "-m", "venv", str(venv)])
    if editable:
        _run([str(_venv_python(venv)), "-m", "pip", "install", "-e", spec])
    else:  # pip always rebuilds a local directory requirement
        _run([str(_venv_python(venv)), "-m", "pip", "install", "--upgrade", spec])
    return "venv+pip"


def _skill_version(source: Path | None = None) -> str:
    init = Path(source if source is not None else SKILL_SOURCE) / "scripts" / "ultron" / "__init__.py"
    try:
        match = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
    except OSError:
        match = None
    if match is None:
        init = SKILL_SOURCE / "scripts" / "ultron" / "__init__.py"
        match = re.search(r'__version__\s*=\s*"([^"]+)"', init.read_text(encoding="utf-8"))
    return match.group(1) if match else "unknown"


def runtime_info(*, mode: str = "copy", source: Path | None = None) -> dict:
    """Everything written into RUNTIME.md and the marker. ``mode``: ``copy`` or ``dev``."""
    home = ultron_home()
    return {
        "executable": ultron_executable(home / "venv"),
        "home": home,
        "version": _skill_version(source),
        "installed_at": datetime.datetime.now(datetime.UTC).replace(microsecond=0).isoformat(),
        "mode": mode,
    }


def runtime_text(executable: Path, home: Path, version: str | None = None, installed_at: str | None = None) -> str:
    text = (
        "# ULTRON runtime\n\n"
        "Written by `install.py`. Run every ULTRON command with this executable (absolute path):\n\n"
        f"```\n{executable}\n```\n\n"
        f"State folder (`ULTRON_HOME`): `{home}`\n\n"
        "Always quote the path (it may contain spaces). In PowerShell use the call operator, e.g.\n"
        f'`& "{executable}" version`; in bash/zsh: `"{executable}" version`.\n'
    )
    if version or installed_at:
        text += f"\nULTRON version: {version or 'unknown'} - installed: {installed_at or 'unknown'}\n"
    if home.resolve() != (Path.home() / ".ultron").resolve():
        text += f"\nThis is not the default `~/.ultron`: set the environment variable `ULTRON_HOME={home}` for every command.\n"
    return text


# ------------------------------------------------------------------------------------------ copy guard
#
# Ownership rules (nothing outside the ``ultron`` folders this script created is ever deleted):
# - A destination whose real location is the skill source, inside it, one of its parents, or inside the repo's
#   ``skill/`` tree is refused outright (``dest_is_source``), whatever markers it holds.
# - A real folder is ours only if ``.ultron-install.json`` parses, names ``ultron`` and its ``path`` resolves to
#   the folder itself (a copied/planted marker does not qualify).
# - A ``--dev`` symlink is ours only if ``$ULTRON_HOME/installs.json`` records that link with that target. No
#   marker is written through the link (it would land in the source); ``RUNTIME.md`` still is, because the agent
#   reads it next to ``SKILL.md`` and the source copy is git-ignored (phase-1 behaviour).
# - Unmarked phase-1 folders are never deleted: install moves them to ``$ULTRON_HOME/backups/legacy-skill-*``.

STAGING_RE = re.compile(r"^\.ultron-new-[A-Za-z0-9_]+$")
REGISTRY = "installs.json"
HOME_MARKER = ".ultron-home"


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (os.name == "nt" and path.is_junction())


def _real_location(path: Path) -> Path:
    """Where ``path`` itself lives (parents resolved, the last component not followed)."""
    return Path(path).parent.resolve() / Path(path).name


def _overlaps_source(dst: Path, src: Path | None = None) -> bool:
    real = _real_location(dst)
    sources = {SKILL_SOURCE.resolve()}
    if src is not None:
        sources.add(Path(src).resolve())
    skill_tree = (REPO / "skill").resolve()
    if real == skill_tree or skill_tree in real.parents:
        return True
    return any(real == s or s in real.parents or real in s.parents for s in sources)


def _source_error(dst: Path) -> InstallError:
    return InstallError(f"dest_is_source: {dst} resolves into the ULTRON source tree; refusing to touch it")


def _read_marker(path: Path) -> dict | None:
    try:
        data = json.loads((path / MARKER).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _registry_path() -> Path:
    return ultron_home() / REGISTRY


def _read_registry() -> dict:
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    links = data.get("links") if isinstance(data, dict) else None
    return links if isinstance(links, dict) else {}


def _write_registry(links: dict) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"links": links}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _register_link(dst: Path, target: Path) -> None:
    links = _read_registry()
    links[str(_real_location(dst))] = str(Path(target).resolve())
    _write_registry(links)


def _forget_link(dst: Path) -> None:
    links = _read_registry()
    if links.pop(str(_real_location(dst)), None) is not None:
        _write_registry(links)


def _has_marker(path: Path) -> bool:
    """True when ``path`` is an install this script made (see the ownership rules above)."""
    path = Path(path)
    if _overlaps_source(path):
        return False
    if _is_link(path):
        target = _read_registry().get(str(_real_location(path)))
        try:
            return bool(target) and Path(target).resolve() == path.resolve()
        except OSError:
            return False
    if not path.is_dir():
        return False
    marker = _read_marker(path)
    if not marker or marker.get("name") != "ultron" or not isinstance(marker.get("path"), str):
        return False
    try:
        return Path(marker["path"]).resolve() == path.resolve()
    except OSError:
        return False


def _is_legacy_install(path: Path) -> bool:
    """Phase-1 installs had no marker: ``SKILL.md`` named ultron plus a ``RUNTIME.md`` written by install.py."""
    try:
        skill = (path / "SKILL.md").read_text(encoding="utf-8")
        runtime = (path / "RUNTIME.md").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return bool(re.search(r"^name:\s*ultron\s*$", skill, re.MULTILINE)) and runtime.startswith("# ULTRON runtime")


def _state(dst: Path, src: Path | None = None) -> str:
    """``absent`` | ``source`` | ``link`` (ours) | ``ours`` | ``legacy`` (unmarked phase-1 folder) | ``foreign``."""
    if _overlaps_source(dst, src):
        return "source"
    if _is_link(dst):
        # phase-1 dev links had no marker and always pointed at this repo's skill/ultron; unlinking loses nothing
        legacy = _is_legacy_install(dst) and dst.resolve() == SKILL_SOURCE.resolve()
        return "link" if (_has_marker(dst) or legacy) else "foreign"
    if not dst.exists():
        return "absent"
    if dst.is_dir() and _has_marker(dst):
        return "ours"
    if dst.is_dir() and _is_legacy_install(dst):
        return "legacy"
    return "foreign"


STATUS_FOR_STATE = {"absent": "installed", "link": "updated", "ours": "updated", "legacy": "upgraded-legacy"}


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except (IsADirectoryError, PermissionError):
        path.rmdir()  # Windows directory symlinks / junctions: removes the link, never the target


def _foreign_error(dst: Path) -> InstallError:
    return InstallError(f"{dst} exists and is not an ULTRON install (no valid {MARKER}); remove it manually")


def _state_error(dst: Path, state: str) -> InstallError | None:
    if state == "source":
        return _source_error(dst)
    if state == "foreign":
        return _foreign_error(dst)
    return None


def _write_runtime(dst: Path, runtime: dict, src: Path) -> None:
    executable, home = Path(runtime["executable"]), Path(runtime["home"])
    text = runtime_text(executable, home, runtime.get("version"), runtime.get("installed_at"))
    (dst / "RUNTIME.md").write_text(text, encoding="utf-8")
    if _is_link(dst):  # never write the marker through a link into the source; the registry records the link
        _register_link(dst, src)
        return
    marker = {
        "name": "ultron",
        "installer": "install.py",
        "version": runtime.get("version"),
        "installed_at": runtime.get("installed_at"),
        "mode": "copy",
        "path": str(dst),
        "source": str(src),
        "ultron": str(executable),
        "ultron_home": str(home),
    }
    (dst / MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _legacy_backup_dir(label: str) -> Path:
    """``$ULTRON_HOME/backups/legacy-skill-<label>-<YYYYmmdd-HHMMSS>`` (outside every skills folder, so agents
    never load the backup as a second ``ultron`` skill)."""
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_+-]", "_", label) or "skill"
    base = ultron_home() / "backups"
    candidate = base / f"legacy-skill-{safe}-{stamp}"
    n = 1
    while candidate.exists() or _is_link(candidate):
        candidate = base / f"legacy-skill-{safe}-{stamp}-{n}"
        n += 1
    return candidate


def _tree_signature(root: Path) -> list[tuple[str, int]]:
    """Relative paths and sizes of every file/link in ``root`` (links not followed)."""
    entries = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames + [d for d in dirnames if _is_link(Path(dirpath) / d)]:
            path = Path(dirpath) / name
            size = 0 if _is_link(path) else path.stat().st_size
            entries.append((path.relative_to(root).as_posix(), size))
    return sorted(entries)


def _move_legacy(dst: Path, label: str) -> Path:
    """Move an unmarked phase-1 folder into ``$ULTRON_HOME/backups``; never delete it before a verified copy."""
    backup = _legacy_backup_dir(label)
    backup.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(dst, backup)
        return backup
    except OSError:
        pass  # e.g. ULTRON_HOME on another drive: copy, verify, then delete the original
    shutil.copytree(dst, backup, symlinks=True)
    if _tree_signature(backup) != _tree_signature(dst):
        shutil.rmtree(backup, ignore_errors=True)  # our incomplete copy; the original is untouched
        raise InstallError(f"could not back up {dst} to {backup} (copy did not verify); nothing was changed")
    shutil.rmtree(dst)
    return backup


def _clean_stale_staging(parent: Path) -> None:
    """Remove ``.ultron-new-*`` folders left by an interrupted install (exact pattern, real folders only)."""
    if not parent.is_dir():
        return
    for child in parent.iterdir():
        if STAGING_RE.fullmatch(child.name) and child.is_dir() and not _is_link(child):
            shutil.rmtree(child, ignore_errors=True)


def _make_staging(parent: Path) -> Path:
    """Create ``parent/.ultron-new-<random>`` with default permissions.

    Not ``tempfile.mkdtemp``: it makes a private folder (0o700; on Windows a protected owner-only ACL since
    Python 3.12.4) and the staged copy keeps it once moved into place, so other accounts on the machine (e.g.
    Codex's Windows sandbox user) could not read the installed skill.
    """
    for _ in range(100):
        staging = parent / f".ultron-new-{secrets.token_hex(6)}"
        try:
            staging.mkdir()
        except FileExistsError:
            continue
        return staging
    raise InstallError(f"could not create a staging folder in {parent}")


def _copy_skill_file(src: str, dst: str) -> str:
    """Create files with destination ACLs while preserving file times and mode."""
    # On Windows, copy2 can use CopyFile2, which also copies source security attributes.
    shutil.copyfile(src, dst)
    shutil.copystat(src, dst)
    return dst


def install_copy(src: Path, dst: Path, runtime: dict, *, label: str | None = None, info: dict | None = None) -> str:
    """Install ``src`` at ``dst``; return ``installed``, ``updated`` or ``upgraded-legacy``.

    Our previous install is replaced; an unmarked phase-1 folder is moved to
    ``$ULTRON_HOME/backups/legacy-skill-<label>-<timestamp>`` (its path goes to ``info["backup"]``);
    anything else (or a destination inside the source tree) raises ``InstallError``. Links are unlinked, never
    followed. The new copy is staged in ``.ultron-new-*`` next to ``dst``; the old one is moved into the staging
    folder, the new one renamed into place (old one restored if that fails), and only then is the old one deleted.
    ``runtime["mode"] == "dev"`` tries a symlink to ``src`` first.
    """
    src, dst = Path(src).resolve(), Path(dst)
    state = _state(dst, src)
    error = _state_error(dst, state)
    if error:
        raise error
    status = STATUS_FOR_STATE[state]
    dst.parent.mkdir(parents=True, exist_ok=True)
    _clean_stale_staging(dst.parent)
    if state == "legacy":
        backup = _move_legacy(dst, label or dst.parent.parent.name.lstrip(".") or "skill")
        if info is not None:
            info["backup"] = str(backup)
        state = "absent"
    if runtime.get("mode") == "dev":
        _discard(dst, state)
        try:
            os.symlink(src, dst, target_is_directory=True)
        except (OSError, NotImplementedError):  # e.g. Windows without Developer Mode / symlink privilege
            shutil.copytree(src, dst, ignore=COPY_IGNORE, copy_function=_copy_skill_file)
        _write_runtime(dst, runtime, src)
        return status
    staging = _make_staging(dst.parent)
    staged, old = staging / "ultron", staging / "old"
    try:
        shutil.copytree(src, staged, ignore=COPY_IGNORE, copy_function=_copy_skill_file)
        if state == "link":
            _unlink(dst)
            _forget_link(dst)
        elif state == "ours":
            dst.rename(old)  # move the previous install aside; deleted only once the new one is in place
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)  # holds only the partial new copy
        raise
    try:
        staged.rename(dst)
    except OSError as exc:
        if old.exists():
            try:
                old.rename(dst)  # put the previous install back
            except OSError:
                raise InstallError(f"could not finish installing {dst}; previous install kept in {old}") from exc
        shutil.rmtree(staging, ignore_errors=True)
        raise
    shutil.rmtree(staging, ignore_errors=True)  # our staging folder, now holding only the old copy
    _write_runtime(dst, runtime, src)
    return status


# Claude Code shows ``argument-hint`` next to ``/ultron`` while typing. Other platforms may reject frontmatter keys
# they do not know, so only the Claude Code copy gets it (never a dev symlink: that would edit the source).
CLAUDE_ARGUMENT_HINT = 'argument-hint: "[search|profile|jobs|runs|system] [action] [args]"'


def _add_claude_argument_hint(dst: Path) -> None:
    skill_md = dst / "SKILL.md"
    if _is_link(dst) or not skill_md.is_file():
        return
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---") or "argument-hint:" in text.split("---", 2)[1]:
        return
    head, sep, rest = text.partition("\n")
    skill_md.write_text(f"{head}{sep}{CLAUDE_ARGUMENT_HINT}\n{rest}", encoding="utf-8")


def _discard(dst: Path, state: str) -> None:
    if state == "link":
        _unlink(dst)
        _forget_link(dst)
    elif state == "ours":
        shutil.rmtree(dst)


def uninstall_copy(dst: Path) -> bool:
    """Remove ``dst`` only if it is ours (valid marker, or a registered dev link, which is unlinked)."""
    dst = Path(dst)
    if not _has_marker(dst):
        return False
    if _is_link(dst):
        _unlink(dst)
        _forget_link(dst)
    elif dst.is_dir():
        shutil.rmtree(dst)
    else:
        return False
    return True


def _uninstall_reason(dst: Path) -> str:
    if _overlaps_source(dst):
        return str(_source_error(dst))
    if not dst.exists() and not _is_link(dst):
        return "not installed"
    if _is_legacy_install(dst):
        return f"phase-1 install without {MARKER}; re-run install.py to upgrade it, or remove it manually"
    return f"no valid {MARKER}: not created by install.py, left untouched"


def _mark_home(home: Path) -> None:
    """Record that this folder is an ULTRON home (only then does ``--purge-data`` delete its generic entries).

    Only a folder the installer creates, or one that is empty, is marked: an existing folder with other
    content (``ULTRON_HOME`` pointed at a shared directory) is never claimed.
    """
    marker = home / HOME_MARKER
    if marker.exists():
        return
    claim = not home.exists() or (home.is_dir() and not _is_link(home) and not any(home.iterdir()))
    home.mkdir(parents=True, exist_ok=True)
    if claim:
        marker.write_text("ULTRON home created by install.py\n", encoding="utf-8")


# ------------------------------------------------------------------------------------------- dev mode


def install_dev(*, target: Path, source: Path | None = None, force_copy: bool = False) -> dict:
    """Phase-1 developer install: editable venv + ``<target>/.claude`` and ``<target>/.agents`` links."""
    source = Path(source) if source is not None else SKILL_SOURCE
    home = ultron_home()
    venv = home / "venv"
    tool = "skipped"
    _mark_home(home)
    if os.environ.get("ULTRON_INSTALL_SKIP_VENV") != "1":
        tool = create_venv(venv, source / "scripts", editable=True)
    runtime = runtime_info(mode="copy" if force_copy else "dev", source=source)
    targets = []
    for rel in PLATFORM_DIRS:
        dest = Path(target) / rel / "ultron"
        _exit_if_terminated()
        install_copy(source, dest, runtime, label=rel.parts[0].lstrip("."))
        targets.append({"path": str(dest), "mode": "symlink" if _is_link(dest) else "copy"})
    return {"venv": str(venv), "venv_tool": tool, "ultron": str(runtime["executable"]), "targets": targets}


# ---------------------------------------------------------------------------------------- full flows


def run_install(*, scope: str, project: Path | None, platforms: list[str], dev: bool = False,
                force_copy: bool = False, update: bool = False, dry_run: bool = False,
                source: Path | None = None) -> dict:
    source = Path(source) if source is not None else SKILL_SOURCE
    home = ultron_home()
    venv = home / "venv"
    runtime = runtime_info(mode="dev" if dev and not force_copy else "copy", source=source)
    entries = []
    for path, names in _group_by_path(platform_targets(scope, project, platforms)):
        entry: dict = {"platforms": names, "path": str(path)}
        state = _state(path, source)
        error = _state_error(path, state)
        if error:
            entry.update(status="skipped", reason=str(error))
        elif update and state == "absent":
            entry.update(status="skipped", reason="not installed")
        elif dry_run:
            entry["status"] = STATUS_FOR_STATE[state]
        else:
            entry["status"] = "pending"
        entries.append(entry)
    tool = "skipped"
    if not dry_run:
        _mark_home(home)
        if os.environ.get("ULTRON_INSTALL_SKIP_VENV") != "1":
            tool = create_venv(venv, source / "scripts", editable=dev)
    for entry in entries:
        if entry["status"] != "pending":
            continue
        dest = Path(entry["path"])
        _exit_if_terminated()
        try:
            label = entry["platforms"][0] if len(entry["platforms"]) == 1 else "agents"
            entry["status"] = install_copy(source, dest, runtime, label=label, info=entry)
            if entry["platforms"] == ["claude"]:
                _add_claude_argument_hint(dest)
            entry["mode"] = "symlink" if _is_link(dest) else "copy"
        except (InstallError, OSError) as exc:
            entry.update(status="skipped", reason=str(exc))
    _exit_if_terminated()
    report = {"action": "update" if update else "install", "scope": scope, "dry_run": dry_run,
              "version": runtime["version"], "venv": str(venv), "venv_tool": tool,
              "ultron": str(runtime["executable"]), "targets": entries}
    if not dry_run:
        report["ok"] = not any(e["status"] == "skipped" and e.get("reason") != "not installed" for e in entries)
    return report


def _home_refusal(home: Path) -> str | None:
    """Why ``home`` must not be purged, or ``None`` when it looks like an ULTRON home."""
    if _is_link(home):
        return "ULTRON_HOME is a link; remove it manually"
    resolved = home.resolve()
    user = Path.home().resolve()
    if resolved == Path(resolved.anchor) or resolved == user or resolved in user.parents:
        return f"{home} is the user's home, one of its parents or a drive root"
    if resolved == REPO or resolved in REPO.parents or REPO in resolved.parents:
        return f"{home} overlaps the ULTRON source folder"
    # P4-R3: only the installer's own marker or the ULTRON database qualify (a venv or runs/ alone does not);
    # with the database alone, _purge_home deletes only ULTRON-specific names
    looks_like_home = (home / HOME_MARKER).is_file() or (home / "ultron.sqlite3").is_file()
    if not looks_like_home:
        return f"{home} does not look like an ULTRON home (no {HOME_MARKER} or ultron.sqlite3)"
    return None


# Entries of $ULTRON_HOME that ULTRON creates; ``--purge-data`` deletes only these (C1).
OWNED_HOME_ENTRIES = frozenset({
    "venv", "runs", "profiles", "seeds", "backups", "cv", "ultron.sqlite3", "ultron.sqlite3-wal",
    "ultron.sqlite3-shm", "ultron.sqlite3-journal", "update-notices", REGISTRY, HOME_MARKER,
})
# Without HOME_MARKER the folder existed before ULTRON used it (the CLI creates the ledger in any folder
# ULTRON_HOME points at), so generic names like venv/ or cv/ may be the user's: only these are deleted.
SPECIFIC_HOME_ENTRIES = frozenset({
    "ultron.sqlite3", "ultron.sqlite3-wal", "ultron.sqlite3-shm", "ultron.sqlite3-journal", "update-notices",
    REGISTRY,
})


def _purge_home(home: Path, *, dry_run: bool) -> dict:
    """Delete the ULTRON-owned entries of ``home``; remove ``home`` itself only if nothing else is left."""
    deletable = OWNED_HOME_ENTRIES if (home / HOME_MARKER).is_file() else SPECIFIC_HOME_ENTRIES
    children = sorted(home.iterdir(), key=lambda p: p.name)
    owned = [c for c in children if c.name in deletable]
    foreign = [c.name for c in children if c.name not in deletable]
    extra = {"would_delete": [c.name for c in owned]} if dry_run else {}
    if not dry_run:
        # The marker goes last: if an earlier deletion fails, a retry still treats the generic entries as ULTRON's
        for child in sorted(owned, key=lambda c: c.name == HOME_MARKER):
            if _is_link(child):
                _unlink(child)  # never follow a link out of the home
            elif child.is_dir():
                shutil.rmtree(child)  # rmtree unlinks inner symlinks, never follows them
            else:
                child.unlink()
        if not foreign:
            home.rmdir()
    if not foreign:
        return {"status": "removed", **extra}
    note = "the folder also holds files ULTRON did not create; they and the folder were kept"
    if deletable is SPECIFIC_HOME_ENTRIES:
        note = (f"no {HOME_MARKER}: the folder existed before ULTRON used it, so only ULTRON's database and "
                "records were deleted; the rest and the folder were kept (remove them manually if they are ULTRON's)")
    return {"status": "ultron_data_removed", "kept_foreign_entries": foreign, **extra, "note": note}


def run_uninstall(*, scope: str, project: Path | None, platforms: list[str], purge_data: bool = False,
                  dry_run: bool = False) -> dict:
    entries = []
    for path, names in _group_by_path(platform_targets(scope, project, platforms)):
        entry: dict = {"platforms": names, "path": str(path)}
        if not _has_marker(path):
            entry.update(status="skipped", reason=_uninstall_reason(path))
        elif dry_run:
            entry["status"] = "removed"
        else:
            _exit_if_terminated()
            try:
                entry["status"] = "removed" if uninstall_copy(path) else "skipped"
            except OSError as exc:
                entry.update(status="failed", reason=str(exc))
        entries.append(entry)
    home = ultron_home()
    data: dict = {"path": str(home)}
    if not home.exists() and not _is_link(home):
        data["status"] = "absent"
    elif not purge_data:
        data.update(status="kept", hint="your searches, history and venv stay here; re-run with "
                    "`--uninstall --purge-data` to delete ULTRON's data in this folder")
    else:
        refusal = _home_refusal(home)
        if refusal:
            data.update(status="refused", reason=refusal)
        else:
            _exit_if_terminated()  # never start deleting data after a termination request
            data.update(_purge_home(home, dry_run=dry_run))
    _exit_if_terminated()
    # Folders we did not create are skipped on purpose; only a failed removal or a refused purge is an error.
    ok = data["status"] != "refused" and not any(e["status"] == "failed" for e in entries)
    return {"action": "uninstall", "scope": scope, "dry_run": dry_run, "targets": entries,
            "ultron_home": data, "ok": ok}


# ----------------------------------------------------------------------------------------------- CLI


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # stdout must stay a single JSON object
        raise UsageError(message)


def _parse_platforms(value: str | None) -> list[str] | None:
    if value is None:
        return None
    names = [part.strip().lower() for part in value.split(",") if part.strip()]
    unknown = [name for name in names if name not in PLATFORMS]
    if unknown or not names:
        raise UsageError(f"unknown platform(s) {unknown or value!r}; choose from {', '.join(PLATFORMS)}")
    return [name for name in PLATFORMS if name in names]


def _dump(report: dict) -> str:
    """Pretty JSON with one line per target so five targets stay well under ~40 lines."""
    targets = report.get("targets")
    if not targets:
        return json.dumps(report, ensure_ascii=False, indent=2)
    head = json.dumps({k: v for k, v in report.items() if k != "targets"}, ensure_ascii=False, indent=2)
    rows = ",\n".join("    " + json.dumps(t, ensure_ascii=False) for t in targets)
    return head[:-2] + ',\n  "targets": [\n' + rows + "\n  ]\n}"


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Install, update or uninstall ULTRON.")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--global", dest="global_", action="store_true", help="install into user folders (default)")
    scope.add_argument("--project", metavar="DIR", help="install into DIR/.claude and DIR/.agents")
    scope.add_argument("--target", metavar="DIR", help="phase-1 alias of --project (used with --dev)")
    parser.add_argument("--platforms", help="comma list: claude,codex,opencode,antigravity")
    parser.add_argument("--dev", action="store_true", help="editable venv install; symlink the skill when possible")
    parser.add_argument("--copy", action="store_true", help="with --dev: copy the skill instead of symlinking")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--update", action="store_true", help="refresh the venv and existing installs only")
    action.add_argument("--uninstall", action="store_true", help="remove installs that carry the ULTRON marker")
    parser.add_argument("--purge-data", action="store_true",
                        help="with --uninstall: also delete ULTRON's data in $ULTRON_HOME (other files are kept)")
    parser.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.version_info < (3, 12):  # noqa: UP036 - install.py may be launched by an older interpreter
        version = ".".join(map(str, sys.version_info[:3]))
        detail = f"ULTRON needs Python >= 3.12 (running {version}). {PYTHON_HINT}"
        print(json.dumps({"error": "python_too_old", "detail": detail}))
        return 1
    try:
        args = _build_parser().parse_args(argv)
        if args.purge_data and not args.uninstall:
            raise UsageError("--purge-data only works with --uninstall")
        if args.copy and not args.dev:
            raise UsageError("--copy only works with --dev")
        platforms = _parse_platforms(args.platforms)
    except UsageError as exc:
        print(json.dumps({"error": "usage", "detail": str(exc)}, ensure_ascii=False))
        return 2

    project_dir = args.project or args.target
    if project_dir is not None:
        scope, project = "project", Path(project_dir).expanduser().resolve()
    elif args.dev and not args.global_:
        scope, project = "project", Path.cwd()  # phase-1 behaviour: --dev installs into --target (.)
    else:
        scope, project = "global", None
    if platforms is None:
        # Installing globally targets the platforms present; update/uninstall look at every known folder.
        platforms = detect_platforms() if scope == "global" and not (args.update or args.uninstall) else list(PLATFORMS)

    try:
        if args.uninstall:
            report = run_uninstall(scope=scope, project=project, platforms=platforms,
                                   purge_data=args.purge_data, dry_run=args.dry_run)
        else:
            report = run_install(scope=scope, project=project, platforms=platforms, dev=args.dev,
                                 force_copy=args.copy, update=args.update, dry_run=args.dry_run)
    except (InstallError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": "install_failed", "detail": str(exc)}, ensure_ascii=False))
        return 1
    except Terminated as exc:
        detail = f"{exc}; work finished before the signal is kept, nothing after it was started"
        print(json.dumps({"error": "terminated", "detail": detail}, ensure_ascii=False))
        return exc.code
    print(_dump(report))
    return 0 if report.get("ok", True) else 1


if __name__ == "__main__":
    _handle_termination_signals()
    sys.exit(main())
