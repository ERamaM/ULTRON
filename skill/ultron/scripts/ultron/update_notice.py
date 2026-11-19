"""Optional, quiet notices for published ULTRON releases."""

from __future__ import annotations

import os
import re
import time

import httpx

from ultron.home import ultron_home

_REPOSITORY = "https://github.com/ERamaM/ULTRON"
_LATEST_RELEASE = "https://api.github.com/repos/ERamaM/ULTRON/releases/latest"
_VERSION = re.compile(r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
_CHECK_INTERVAL = 24 * 60 * 60


def release_notice(installed_version: str) -> dict[str, str] | None:
    """Return a notice once per newer stable release, with a daily network check."""
    installed = _VERSION.fullmatch(installed_version)
    if installed is None or os.getenv("ULTRON_DISABLE_UPDATE_CHECK") == "1":
        return None

    try:
        state = ultron_home() / "update-notices"
        state.mkdir(exist_ok=True)
        checked = state / "last-checked"
        if checked.exists() and 0 <= time.time() - checked.stat().st_mtime < _CHECK_INTERVAL:
            return None
        checked.touch()
        response = httpx.get(_LATEST_RELEASE, timeout=1.5)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
            return None
        tag = payload.get("tag_name")
        available = _VERSION.fullmatch(tag) if isinstance(tag, str) else None
        if not available or tuple(map(int, available.groups())) <= tuple(map(int, installed.groups())):
            return None
        version = ".".join(available.groups())
        (state / version).touch(exist_ok=False)
    except (OSError, ValueError, httpx.HTTPError):
        return None

    return {"version": version, "url": f"{_REPOSITORY}/releases/tag/{tag}"}
