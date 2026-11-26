"""Read the latest bounded watch closure snapshots without following symlinks."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path

_DIGEST_NAME = re.compile(r"(.+)-([0-9]{8}T[0-9]{6})\.closed\.json", re.ASCII)
_MAX_DIGEST_BYTES = 256 * 1024


def _regular_digest(info: os.stat_result) -> bool:
    return stat.S_ISREG(info.st_mode) and _ordinary_path(info) and info.st_size <= _MAX_DIGEST_BYTES


def _ordinary_path(info: os.stat_result) -> bool:
    return not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def _read_digest(path: Path) -> list:
    try:
        before = path.lstat()
        if not _regular_digest(before):
            return []
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as handle:
            current = os.fstat(handle.fileno())
            if not _regular_digest(current) or not os.path.samestat(before, current):
                return []
            raw = handle.read(_MAX_DIGEST_BYTES + 1)
        if len(raw) > _MAX_DIGEST_BYTES:
            return []
        rows = json.loads(raw)
    except (OSError, ValueError, RecursionError):
        return []
    return rows if isinstance(rows, list) else []


def _latest_digests(folder: Path) -> dict[str, tuple[datetime, Path]]:
    latest: dict[str, tuple[datetime, Path]] = {}
    try:
        folder_info = folder.lstat()
        if not stat.S_ISDIR(folder_info.st_mode) or not _ordinary_path(folder_info):
            return latest
        for path in folder.iterdir():
            match = _DIGEST_NAME.fullmatch(path.name)
            if not match:
                continue
            profile, stamp = match.groups()
            try:
                when = datetime.strptime(stamp, "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
                supported = _regular_digest(path.lstat())
            except (ValueError, OSError):
                continue
            if supported and (profile not in latest or when > latest[profile][0]):
                latest[profile] = (when, path)
    except OSError:
        return {}
    return latest


def latest_closures(folder: Path, profile: str | None = None) -> dict[str, tuple[str, list]]:
    """Read bounded latest digests, optionally selecting a profile before opening any files."""
    return {
        slug: (when.isoformat(), _read_digest(path))
        for slug, (when, path) in sorted(_latest_digests(folder).items())
        if profile is None or slug == profile
    }
