"""``ultron update``: check for a stable release and prepare (never run) the installer that applies it.

The running installation cannot replace itself (on Windows the venv's ``ultron.exe`` is locked while it runs), so
``prepare_update`` only downloads and verifies the release's source archive and returns the ``install.py --update``
command(s) for the agent to run as a separate process. Nothing from the archive is executed here.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import sys
import tempfile
import zipfile
import zlib
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ultron import __version__
from ultron.doctor import _home_path, _marker_valid, _registered_link
from ultron.doctor import _in_dev_checkout as _doctor_in_dev_checkout
from ultron.net import USER_AGENT
from ultron.platforms import PLATFORM_NAMES, global_skill_dirs
from ultron.update_notice import format_version, latest_release, parse_version, release_for_version, repository

API_TIMEOUT = 10.0
DOWNLOAD_TIMEOUT = 60.0
MAX_REDIRECTS = 3
MAX_DOWNLOAD_BYTES = 50_000_000
MAX_EXTRACTED_BYTES = 200_000_000
MAX_MEMBERS = 10_000
DOWNLOAD_HOSTS = frozenset({"github.com", "codeload.github.com"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
PROJECT_AGENTS_PLATFORMS = ("codex", "opencode", "antigravity")  # share DIR/.agents/skills/ultron
_VERSION_LINE = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE)

# Test hook: an ``httpx`` transport for the archive download.
_TRANSPORT: httpx.BaseTransport | None = None


class UpdateError(RuntimeError):
    """A failure with its JSON error code and exit code."""

    def __init__(self, error: str, detail: str, code: int = 1) -> None:
        super().__init__(detail)
        self.error = error
        self.code = code


class DownloadError(RuntimeError):
    pass


class ArchiveError(ValueError):
    pass


def _in_dev_checkout() -> bool:
    return _doctor_in_dev_checkout()


# ----------------------------------------------------------------------------------------------- check


def check() -> dict:
    """Installed vs latest stable release. Raises ``ReleaseLookupError`` / ``InvalidRepository``."""
    release = latest_release(timeout=API_TIMEOUT)
    installed = parse_version(__version__)
    available = release is not None and (installed is None or release["version"] > installed)
    return {
        "installed": __version__,
        "latest": format_version(release["version"]) if release else None,
        "update_available": available,
        "release_url": release["url"] if release else None,
    }


# --------------------------------------------------------------------------------------------- scopes


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (os.name == "nt" and path.is_junction())


def _is_copy_install(path: Path) -> bool:
    """A real folder with a valid installer marker; links (dev installs) never qualify."""
    return not _is_link(path) and path.is_dir() and _marker_valid(path)


def _ordered(platforms: set[str]) -> list[str]:
    return [name for name in PLATFORM_NAMES if name in platforms]


def _project_dirs(home: Path, cwd: Path, projects: list[Path]) -> list[Path]:
    """The current folder plus ``--project`` folders, resolved and deduplicated (the home is global scope)."""
    out: list[Path] = []
    for folder in [cwd, *projects]:
        resolved = Path(folder).expanduser().resolve()
        if resolved != Path(home).resolve() and resolved not in out:
            out.append(resolved)
    return out


def _project_skill_dirs(project: Path) -> dict[tuple[str, ...], Path]:
    return {
        ("claude",): project / ".claude" / "skills" / "ultron",
        PROJECT_AGENTS_PLATFORMS: project / ".agents" / "skills" / "ultron",
    }


def _real(path: Path) -> str:
    """The key ``install.py`` uses in ``installs.json`` (parents resolved, last component not followed)."""
    return str(Path(path).parent.resolve() / Path(path).name)


def _registry_records() -> dict:
    """``installs`` from ``$ULTRON_HOME/installs.json`` (written by install.py); empty for older/missing files."""
    try:
        data = json.loads((_home_path() / "installs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    records = data.get("installs") if isinstance(data, dict) else None
    return records if isinstance(records, dict) else {}


def _marker_version(path: Path) -> str | None:
    try:
        version = json.loads((path / ".ultron-install.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def _platforms(names: object) -> list[str]:
    if not isinstance(names, list):
        return []
    return _ordered({"antigravity" if n == "antigravity-legacy" else n for n in names if isinstance(n, str)})


def _recorded_copies() -> list[dict]:
    """Copy installs recorded by install.py that still exist and still carry a valid marker."""
    copies = []
    for record in _registry_records().values():
        if not isinstance(record, dict) or record.get("mode") == "link" or not isinstance(record.get("path"), str):
            continue
        scope, project, platforms = record.get("scope"), record.get("project"), _platforms(record.get("platforms"))
        if scope not in ("global", "project") or not platforms or (scope == "project" and not isinstance(project, str)):
            continue
        path = Path(record["path"])
        if not _is_copy_install(path):  # removed or replaced since it was recorded
            continue
        version = record.get("version") if isinstance(record.get("version"), str) else _marker_version(path)
        copies.append({"scope": scope, "project": project if scope == "project" else None, "path": str(path),
                       "platforms": platforms, "version": version, "found_by": "record"})
    return copies


def find_copies(*, home: Path, cwd: Path, projects: list[Path]) -> list[dict]:
    """Every copy install to consider, one entry per skill folder.

    Records in ``installs.json`` come first (they find project installs anywhere). Installs made before
    install.py recorded them are found by their ``.ultron-install.json`` marker in the platforms' global folders,
    ``cwd`` and ``projects``.
    """
    copies = _recorded_copies()
    seen = {_real(Path(c["path"])) for c in copies}

    def add(scope: str, project: str | None, path: Path, platforms: list[str]) -> None:
        if _real(path) not in seen and _is_copy_install(path):
            seen.add(_real(path))
            copies.append({"scope": scope, "project": project, "path": str(path), "platforms": platforms,
                           "version": _marker_version(path), "found_by": "marker"})

    for name, path in global_skill_dirs(home).items():
        add("global", None, path, ["antigravity" if name == "antigravity-legacy" else name])
    for project in _project_dirs(home, cwd, projects):
        for platforms, path in _project_skill_dirs(project).items():
            add("project", str(project), path, list(platforms))
    return copies


def group_scopes(copies: list[dict]) -> list[dict]:
    """One entry per installer scope (global, or one project folder) with the union of platforms."""
    groups: dict[tuple[str, str | None], dict] = {}
    for copy in copies:
        key = (copy["scope"], copy["project"])
        group = groups.setdefault(key, {"scope": copy["scope"], "platforms": set(), "paths": [], "versions": {}})
        if copy["scope"] == "project":
            group["project"] = copy["project"]
        group["platforms"].update(copy["platforms"])
        group["paths"].append(copy["path"])
        group["versions"][copy["path"]] = copy["version"]
    return [{**group, "platforms": _ordered(group["platforms"])} for group in groups.values()]


def discover_installs(*, home: Path, cwd: Path, projects: list[Path]) -> list[dict]:
    """Installer scopes to refresh (see ``find_copies``)."""
    return group_scopes(find_copies(home=home, cwd=cwd, projects=projects))


def dev_links(*, home: Path, cwd: Path, projects: list[Path]) -> list[str]:
    """Linked skill folders registered in ``installs.json`` (``--dev`` installs): updated with git, not here."""
    candidates = list(global_skill_dirs(home).values())
    for project in _project_dirs(home, cwd, projects):
        candidates.extend(_project_skill_dirs(project).values())
    for record in _registry_records().values():
        if isinstance(record, dict) and record.get("mode") == "link" and isinstance(record.get("path"), str):
            candidates.append(Path(record["path"]))
    out: list[str] = []
    for path in candidates:
        if _is_link(path) and _registered_link(path) and str(path) not in out:
            out.append(str(path))
    return out


# ------------------------------------------------------------------------------------------- commands


def _runner(install_py: Path) -> list[str]:
    """Run ``install.py`` outside ULTRON's venv (the installer rebuilds it), like the bootstrap scripts do."""
    uv = shutil.which("uv")
    if uv:
        return [uv, "run", "--python", ">=3.12", "--no-project", str(install_py)]
    python = getattr(sys, "_base_executable", None) or sys.executable
    return [python, str(install_py)]


def _display(argv: list[str], env: dict[str, str]) -> str:
    """The command as one line for the current shell: PowerShell on Windows, POSIX sh elsewhere."""
    if os.name == "nt":
        quote = "'{}'".format
        prefix = "".join(f"$env:{k} = {quote(v.replace(chr(39), chr(39) * 2))}; " for k, v in env.items())
        return prefix + "& " + " ".join(quote(a.replace("'", "''")) for a in argv)
    prefix = "".join(f"{k}={shlex.quote(v)} " for k, v in env.items())
    return prefix + shlex.join(argv)


def _env() -> dict[str, str]:
    home = os.environ.get("ULTRON_HOME")
    return {"ULTRON_HOME": str(Path(home).expanduser())} if home else {}


def installer_commands(install_py: Path, installs: list[dict]) -> list[dict]:
    """One ``install.py --update`` per scope (``--global`` and ``--project`` are mutually exclusive)."""
    env = _env()
    commands = []
    for install in installs:
        scope_args = ["--global"] if install["scope"] == "global" else ["--project", install["project"]]
        argv = [*_runner(install_py), "--update", *scope_args, "--platforms", ",".join(install["platforms"])]
        command = {"scope": install["scope"], "argv": argv, "display": _display(argv, env)}
        if install["scope"] == "project":
            command["project"] = install["project"]
        commands.append(command)
    return commands


# ------------------------------------------------------------------------------------------- download


def _check_download_url(url: str) -> None:
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise DownloadError("refusing to download from a malformed URL") from exc
    if parts.scheme != "https" or parts.hostname not in DOWNLOAD_HOSTS or parts.username or parts.password:
        raise DownloadError(f"refusing to download from {parts.scheme}://{parts.hostname or ''}")
    if port not in (None, 443):
        raise DownloadError("refusing to download from a non-standard port")


def download_archive(url: str, dest: Path) -> None:
    """Stream ``url`` into ``dest``: https only, GitHub hosts only on every hop, hard size cap."""
    current = url
    try:
        with httpx.Client(transport=_TRANSPORT, follow_redirects=False, trust_env=False,
                          timeout=DOWNLOAD_TIMEOUT, headers={"User-Agent": USER_AGENT}) as client:
            for _hop in range(MAX_REDIRECTS + 1):
                _check_download_url(current)
                with client.stream("GET", current) as response:
                    location = response.headers.get("Location")
                    if response.status_code in REDIRECT_STATUSES and location:
                        current = urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise DownloadError(f"HTTP {response.status_code} from {urlsplit(current).hostname}")
                    declared = response.headers.get("Content-Length", "")
                    if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                        raise DownloadError("release archive is larger than the download limit")
                    total = 0
                    with open(dest, "wb") as out:
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > MAX_DOWNLOAD_BYTES:
                                raise DownloadError("release archive is larger than the download limit")
                            out.write(chunk)
                    return
    except httpx.HTTPError as exc:
        raise DownloadError(f"download failed ({type(exc).__name__})") from exc
    raise DownloadError(f"too many redirects (max {MAX_REDIRECTS})")


# ------------------------------------------------------------------------------------------- extraction


def _member_parts(info: zipfile.ZipInfo) -> list[str]:
    name = info.filename
    if not name or "\\" in name or ":" in name or "\x00" in name or name.startswith("/"):
        raise ArchiveError(f"unsafe path in archive: {name!r}")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        raise ArchiveError(f"unsafe path in archive: {name!r}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ArchiveError(f"archive member is a link: {name!r}")
    return parts


def extract_source(archive: Path, dest: Path, *, expected_version: str) -> Path:
    """Safely extract a GitHub source archive into ``dest``; return its single top-level folder.

    Rejects absolute, drive-qualified, backslash and ``..`` member names, links, duplicate names, more than
    ``MAX_MEMBERS`` entries and more than ``MAX_EXTRACTED_BYTES`` (counted while writing). The folder must hold
    ``install.py`` and ``skill/ultron/SKILL.md`` and declare ``expected_version``.
    """
    dest = Path(dest)
    root = dest.resolve()
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError("the download is not a valid zip archive") from exc
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ArchiveError(f"archive has more than {MAX_MEMBERS} entries")
        planned = [(info, _member_parts(info)) for info in infos]
        tops = {parts[0] for _, parts in planned}
        if len(tops) != 1:
            raise ArchiveError("archive must contain a single top-level folder")
        if sum(info.file_size for info in infos) > MAX_EXTRACTED_BYTES:
            raise ArchiveError("archive is too large once extracted")
        written = 0
        for info, parts in planned:
            target = dest.joinpath(*parts)
            if root != target.resolve() and root not in target.resolve().parents:
                raise ArchiveError(f"unsafe path in archive: {info.filename!r}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                with zf.open(info) as src, open(target, "xb") as out:
                    while chunk := src.read(1 << 16):
                        written += len(chunk)
                        if written > MAX_EXTRACTED_BYTES:
                            raise ArchiveError("archive is too large once extracted")
                        out.write(chunk)
            except FileExistsError as exc:
                raise ArchiveError(f"duplicate archive member: {info.filename!r}") from exc
            except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, zlib.error, RuntimeError,
                    NotImplementedError) as exc:  # corrupt, encrypted or unsupported members
                raise ArchiveError("the archive is corrupt") from exc
    source = dest / tops.pop()
    for required in ("install.py", "skill/ultron/SKILL.md"):
        if not (source / required).is_file():
            raise ArchiveError(f"archive does not contain {required}")
    init = source / "skill" / "ultron" / "scripts" / "ultron" / "__init__.py"
    try:
        match = _VERSION_LINE.search(init.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        match = None
    if match is None or match.group(1) != expected_version:
        found = match.group(1) if match else "none"
        raise ArchiveError(f"archive version {found} does not match the release {expected_version}")
    return source


# --------------------------------------------------------------------------------------------- prepare

NEXT_STEPS = [
    (
        "Ask the user before running anything. If they agree, run each command in `commands` as its own separate "
        "process, one after another, exactly as given (`argv`, with `env` when present)."
    ),
    "Then run `ultron doctor` with the executable listed in RUNTIME.md and report any failed check.",
    "Tell the user to restart the agent session so the updated skill is loaded.",
    "The downloaded source in `cleanup` is no longer needed once the installer has finished; it may be deleted.",
]


def prepare_update(*, to: tuple[int, int, int] | None = None, projects: list[Path] | None = None) -> dict:
    """Resolve the target release, find the installs, download and verify the source, build the commands."""
    projects = list(projects or [])
    if to is not None:
        release = release_for_version(to, timeout=API_TIMEOUT)
        if release is None:
            raise UpdateError("release_not_found", f"v{format_version(to)} is not a published stable release", code=2)
    else:
        release = latest_release(timeout=API_TIMEOUT)
    installed = parse_version(__version__)
    result: dict = {"action": "update", "installed": __version__, "commands": []}
    if release is None:
        result.update(status="no_release", detail="no stable release is published")
        return result
    target = release["version"]
    result.update(target=format_version(target), tag=release["tag"], release_url=release["url"])
    cli_current = installed is not None and (target == installed or (to is None and target < installed))
    if installed is not None and target < installed and to is not None:
        result["downgrade"] = True
    home, cwd = Path.home(), Path.cwd()
    copies = find_copies(home=home, cwd=cwd, projects=projects)
    reason = "cli_outdated"
    if cli_current:
        # The CLI is current; skill copies that an older installer left behind still need the update.
        copies = [c for c in copies if (v := parse_version(c["version"])) is not None and v < target]
        if not copies:
            result.update(status="up_to_date", detail=f"ULTRON {__version__} is already up to date")
            return result
        reason = "stale_skill_copies"
    if _in_dev_checkout():
        result.update(status="source_checkout",
                      detail="running from a source checkout: update it with git, then run install.py --dev --update")
        return result
    installs = group_scopes(copies)
    links = dev_links(home=home, cwd=cwd, projects=projects)
    if links:
        result["dev_links"] = links
        result["dev_note"] = "linked (--dev) skill folders follow their checkout: update it with git instead"
    if not installs:
        result.update(status="no_installs", detail="no ULTRON skill installed by install.py was found (installs.json, "
                      "global folders, the current folder or --project folders)")
        return result
    result["reason"] = reason
    url, _, _ = repository()
    temp = Path(tempfile.mkdtemp(prefix="ultron-update-"))
    try:
        archive = temp / "source.zip"
        download_archive(f"{url}/archive/refs/tags/{release['tag']}.zip", archive)
        (temp / "src").mkdir()
        source = extract_source(archive, temp / "src", expected_version=format_version(target))
        archive.unlink()
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    result.update(
        status="ready",
        source=str(source),
        cleanup=str(temp),
        installs=installs,
        commands=installer_commands(source / "install.py", installs),
        next_steps=NEXT_STEPS,
    )
    if env := _env():
        result["env"] = env
    return result
