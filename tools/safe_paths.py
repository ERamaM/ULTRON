"""File locations for the developer tools.

Reports are always read from and written to one fixed folder, ``<system temp>/ultron-eval``. On the
command line a report is a bare file name, never a path, so no tool argument can point anywhere else.
Report files are run artifacts and stay out of the repository.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

REPORTS_DIR = os.path.join(tempfile.gettempdir(), "ultron-eval")
REPORT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*")


class UnsafePathError(ValueError):
    """A report name is not a plain file name, or a file resolves outside its folder."""


def _report_name(raw: str | os.PathLike[str]) -> str:
    """``raw`` when it is a plain file name (no folder, no ``..``); ``UnsafePathError`` otherwise."""
    text = os.fspath(raw)
    name = os.path.basename(text)
    if name != text or "/" in name or "\\" in name or ".." in name or not REPORT_NAME_RE.fullmatch(name):
        raise UnsafePathError(f"{name or text!r}: give a plain file name; reports live in {REPORTS_DIR}")
    return name


def _check_target(path: str) -> None:
    """An existing report target must be a regular file in the reports folder, never a symlink."""
    if os.path.lexists(path) and (os.path.islink(path) or not os.path.isfile(path) or not inside(path, REPORTS_DIR)):
        raise UnsafePathError(f"{os.path.basename(path)}: existing report target is not a regular file")


def check_report_names(*raw_names: str | os.PathLike[str]) -> None:
    """Validate every report destination before any of them is written."""
    for raw in raw_names:
        _check_target(os.path.join(REPORTS_DIR, _report_name(raw)))


def write_report(raw_name: str | os.PathLike[str], text: str) -> Path:
    """Write ``text`` to ``REPORTS_DIR/<name>`` and return that path."""
    name = _report_name(raw_name)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, name)
    _check_target(path)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return Path(path)


def read_report(raw_name: str | os.PathLike[str]) -> str:
    """The text of ``REPORTS_DIR/<name>``."""
    name = _report_name(raw_name)
    path = os.path.join(REPORTS_DIR, name)
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def inside(path: str, folder: str) -> bool:
    """Whether ``path`` resolves (symlinks included) inside ``folder``."""
    resolved = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(folder))
    return resolved.startswith(root.rstrip(os.sep) + os.sep)
