"""Read-only view of ULTRON installs: the home folder, install markers, registered dev links and the source checkout.

Shared by ``doctor`` and ``update``; nothing here creates or changes files.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import ultron


def home_path() -> Path:
    """``$ULTRON_HOME`` without creating it (``home.ultron_home`` creates subfolders; these checks are read-only)."""
    env = os.environ.get("ULTRON_HOME")
    return Path(env).expanduser() if env else Path.home() / ".ultron"


def marker_valid(path: Path) -> bool:
    try:
        data = json.loads((path / ".ultron-install.json").read_text(encoding="utf-8"))
        return data.get("name") == "ultron" and Path(data["path"]).resolve() == path.resolve()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def registered_link(path: Path) -> bool:
    try:
        links = json.loads((home_path() / "installs.json").read_text(encoding="utf-8")).get("links", {})
        target = links.get(str(path.parent.resolve() / path.name))
        return bool(target) and Path(target).resolve() == path.resolve()
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def in_dev_checkout(package_dir: Path | None = None) -> bool:
    """True when running from the ULTRON source repo: ``install.py`` and the uv-workspace ``pyproject.toml`` sit
    three levels above the ``ultron`` package folder, and the skill folder carries no install marker (an installed
    copy does)."""
    package = Path(package_dir or Path(ultron.__file__).parent).resolve()
    skill = package.parents[1]
    repo = package.parents[3] if len(package.parents) > 3 else None
    if repo is None or (skill / ".ultron-install.json").exists() or not (repo / "install.py").is_file():
        return False
    try:
        return "[tool.uv.workspace]" in (repo / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return False
