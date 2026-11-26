"""Shared filesystem helpers: atomic file replacement and platform-safe link checks."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def is_link(path: Path) -> bool:
    """Return True if ``path`` is a symbolic link or a Windows directory junction."""
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def atomic_write_bytes(path: Path, content: bytes) -> None:
    """Atomically write ``content`` bytes to ``path``, cleaning up the temp file on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 text with LF newlines to ``path``."""
    atomic_write_bytes(path, text.encode("utf-8"))


def atomic_create_bytes(path: Path, content: bytes) -> bool:
    """Publish a private file atomically without replacing an existing entry."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(content)
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)
