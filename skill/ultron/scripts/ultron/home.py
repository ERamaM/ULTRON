"""User-state location ($ULTRON_HOME) and bundled data directory."""

from __future__ import annotations

import os
from pathlib import Path

import ultron

SUBDIRS = ("profiles", "seeds", "runs")


def ultron_home() -> Path:
    """Return ``$ULTRON_HOME`` (default ``~/.ultron``), creating its subfolders."""
    env = os.environ.get("ULTRON_HOME")
    root = Path(env).expanduser() if env else Path.home() / ".ultron"
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
