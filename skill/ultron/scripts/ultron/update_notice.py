"""Published ULTRON releases: stable-release lookup and optional, quiet notices."""

from __future__ import annotations

import os
import re
import time

import httpx

from ultron.home import ultron_home

# Single source of the repository URL; ``ULTRON_REPO`` overrides it, as in install.sh and install.ps1.
DEFAULT_REPOSITORY = "https://github.com/ERamaM/ULTRON"
_REPOSITORY_RE = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?")
_VERSION = re.compile(r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
_CHECK_INTERVAL = 24 * 60 * 60
# File timestamps can run slightly ahead of time.time() (coarser clock on Windows).
_CLOCK_SKEW = 60
NOTICE_TIMEOUT = 1.5


class InvalidRepository(ValueError):
    """``ULTRON_REPO`` is not an https://github.com/<owner>/<repo> URL."""


class ReleaseLookupError(RuntimeError):
    """The GitHub release API could not be reached or returned an unusable answer."""


def repository() -> tuple[str, str, str]:
    """``(url, owner, name)`` of the ULTRON repository (``ULTRON_REPO`` when set)."""
    raw = os.environ.get("ULTRON_REPO") or DEFAULT_REPOSITORY
    match = _REPOSITORY_RE.fullmatch(raw.strip())
    if match is None or match.group(1) in {".", ".."} or match.group(2) in {".", ".."}:
        raise InvalidRepository("ULTRON_REPO must look like https://github.com/<owner>/<repo>")
    owner, name = match.groups()
    return f"https://github.com/{owner}/{name}", owner, name


def parse_version(text: object) -> tuple[int, int, int] | None:
    """``vX.Y.Z`` or ``X.Y.Z`` (no leading zeros, no suffix) as a tuple; ``None`` otherwise."""
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    return tuple(map(int, match.groups())) if match else None  # type: ignore[return-value]


def format_version(version: tuple[int, int, int]) -> str:
    return ".".join(map(str, version))


def _release_from(payload: object) -> dict | None:
    """A stable release as ``{"tag", "version", "url"}``; drafts, prereleases and odd tags give ``None``."""
    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None
    tag = payload.get("tag_name")
    version = parse_version(tag)  # full match: a vX.Y.Z / X.Y.Z tag is safe to put in a URL
    if version is None:
        return None
    url, _, _ = repository()
    return {"tag": tag, "version": version, "url": f"{url}/releases/tag/{tag}"}


def _get_release(path: str, timeout: float) -> dict | None:
    _, owner, name = repository()
    try:
        response = httpx.get(f"https://api.github.com/repos/{owner}/{name}/releases/{path}", timeout=timeout)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ReleaseLookupError(f"GitHub release lookup failed ({type(exc).__name__})") from exc
    return _release_from(payload)


def latest_release(timeout: float = NOTICE_TIMEOUT) -> dict | None:
    """The latest stable release, or ``None`` when there is none. Raises ``ReleaseLookupError`` offline."""
    return _get_release("latest", timeout)


def release_for_version(version: tuple[int, int, int], timeout: float = NOTICE_TIMEOUT) -> dict | None:
    """The stable release tagged ``vX.Y.Z``, or ``None`` when it does not exist or is not stable."""
    return _get_release(f"tags/v{format_version(version)}", timeout)


def release_notice(installed_version: str) -> dict[str, str] | None:
    """Return a notice once per newer stable release, with a daily network check."""
    installed = parse_version(installed_version)
    if installed is None or os.getenv("ULTRON_DISABLE_UPDATE_CHECK") == "1":
        return None

    try:
        state = ultron_home() / "update-notices"
        state.mkdir(exist_ok=True)
        checked = state / "last-checked"
        if checked.exists() and -_CLOCK_SKEW <= time.time() - checked.stat().st_mtime < _CHECK_INTERVAL:
            return None
        checked.touch()
        release = latest_release()
        if release is None or release["version"] <= installed:
            return None
        version = format_version(release["version"])
        (state / version).touch(exist_ok=False)
    except (OSError, ValueError, ReleaseLookupError):
        return None

    return {"version": version, "url": release["url"]}
