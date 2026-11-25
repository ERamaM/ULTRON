"""Read bounded watch closure digests for the ledger beside them."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ultron.exports import sanitize

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


def read_closed_watch(folder: Path, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read the latest closure digest per profile and flag matching ledger jobs."""
    by_url = {job["canonical_url"]: job["identity_key"] for job in jobs if job["canonical_url"]}
    alerts: dict[tuple[str, str], dict[str, Any]] = {}
    for profile, (when, path) in sorted(_latest_digests(folder).items()):
        for row in _read_digest(path):
            if not isinstance(row, dict) or not (url := sanitize.safe_http_url(row.get("apply_url"))):
                continue
            key = by_url.get(url, "")
            alerts.setdefault((profile, key or url), {
                "identity_key": key,
                "title": sanitize.clean_text(row.get("title")),
                "company": sanitize.clean_text(row.get("company")),
                "url": url,
                "profile_slug": sanitize.clean_text(profile),
                "detected_at": when.isoformat(),
            })
    closed_keys = {row["identity_key"] for row in alerts.values() if row["identity_key"]}
    for job in jobs:
        job["is_closed_watch"] = job["identity_key"] in closed_keys
    return list(alerts.values())
