"""Path checks for the developer tools: every CLI-supplied path must stay inside an allowed root."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

REPO_ROOT = os.path.realpath(Path(__file__).resolve().parents[1])


class UnsafePathError(ValueError):
    """A path resolves outside the roots the tool may read or write."""


def default_roots() -> tuple[str, ...]:
    """The repository and the system temporary folder (where pytest and scratch reports live)."""
    return (REPO_ROOT, os.path.realpath(tempfile.gettempdir()))


def _inside(candidate: str, root: str) -> bool:
    candidate = os.path.normcase(candidate)
    root = os.path.normcase(os.path.realpath(root))
    return candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep)


def contained_path(raw: str | os.PathLike[str], roots: tuple[str, ...] | None = None) -> Path:
    """Return ``raw`` fully resolved (symlinks and ``..`` included) if it lies inside one of ``roots``."""
    candidate = os.path.realpath(os.path.abspath(os.fspath(raw)))
    allowed = default_roots() if roots is None else roots
    if not any(_inside(candidate, root) for root in allowed):
        raise UnsafePathError(f"{Path(candidate).name}: path is outside the allowed folders")
    return Path(candidate)
