"""User-state location ($ULTRON_HOME) and bundled data directory."""

from __future__ import annotations

import os
from pathlib import Path

import ultron

SUBDIRS = ("profiles", "seeds", "runs")


def home_path() -> Path:
    """Return the absolute state directory without creating it."""
    env = os.environ.get("ULTRON_HOME")
    return Path(env).expanduser().absolute() if env else Path.home() / ".ultron"


def ultron_home() -> Path:
    """Return ``$ULTRON_HOME`` (default ``~/.ultron``), creating its subfolders."""
    root = home_path()
    for sub in SUBDIRS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def data_dir() -> Path:
    """Bundled data: ``ultron/_data`` in an installed wheel, else ``skill/ultron/data`` in the source tree."""
    package = Path(ultron.__file__).resolve().parent
    bundled = package / "_data"
    if bundled.is_dir():
        return bundled
    return package.parents[1] / "data"
