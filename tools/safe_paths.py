"""File locations for the developer tools.

Reports are always read from and written to one fixed per-user folder,
``<system temp>/ultron-eval-<user>`` (created 0700 and checked to belong to the current user on POSIX).
On the command line a report is a bare file name, never a path, so no tool argument can point anywhere
else. Report files are run artifacts, are redacted before writing, and stay out of the repository.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from ultron.core.privacy import redact


def _user_tag() -> str:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):  # no login name available
        user = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return re.sub(r"[^A-Za-z0-9_-]", "_", user) or "user"


REPORTS_DIR = os.path.join(tempfile.gettempdir(), f"ultron-eval-{_user_tag()}")
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


def _private_reports_dir() -> str:
    """Create the reports folder 0700; refuse one that is a symlink or belongs to another user."""
    os.makedirs(REPORTS_DIR, mode=0o700, exist_ok=True)
    info = os.lstat(REPORTS_DIR)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise UnsafePathError(f"{REPORTS_DIR} is not a plain folder")
    if hasattr(os, "getuid"):
        if info.st_uid != os.getuid():
            raise UnsafePathError(f"{REPORTS_DIR} belongs to another user")
        if stat.S_IMODE(info.st_mode) & 0o077:
            os.chmod(REPORTS_DIR, 0o700)
    return REPORTS_DIR


def _redact_string(text: str) -> str:
    """Redact ``text``; when that changes it, append a stable digest of the original.

    Two identities that differ only in a secret value stay distinct (and match across reports, so
    ``diff`` still pairs them), without revealing the secret.
    """
    redacted = str(redact(text))
    if redacted == text:
        return text
    return f"{redacted}#h:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]}"


def _redact_tree(value: Any) -> Any:
    """Redact every string in a JSON value, dictionary keys included (identity keys are used as keys)."""
    if isinstance(value, dict):
        return {_redact_string(str(key)): _redact_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_tree(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _redacted(text: str) -> str:
    """JSON reports are redacted string by string, so redaction can never consume JSON delimiters."""
    try:
        data = json.loads(text)
    except ValueError:
        return str(redact(text))
    return json.dumps(_redact_tree(data), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_report(raw_name: str | os.PathLike[str], text: str) -> Path:
    """Write redacted ``text`` to ``REPORTS_DIR/<name>`` (0600, never through a symlink); return the path."""
    name = _report_name(raw_name)
    folder = _private_reports_dir()
    path = os.path.join(folder, name)
    _check_target(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(_redacted(text))
    return Path(path)


def read_report(raw_name: str | os.PathLike[str]) -> str:
    """The text of ``REPORTS_DIR/<name>``; a link, or a file resolving outside the folder, is rejected."""
    name = _report_name(raw_name)
    path = os.path.join(REPORTS_DIR, name)
    if os.path.islink(REPORTS_DIR) or os.path.islink(path) or (os.path.lexists(path) and not inside(path, REPORTS_DIR)):
        raise UnsafePathError(f"{name}: report is a link or resolves outside the reports folder")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def remove_report(raw_name: str | os.PathLike[str]) -> bool:
    """Delete ``REPORTS_DIR/<name>`` when it is a regular file there; return whether it existed."""
    name = _report_name(raw_name)
    path = os.path.join(_private_reports_dir(), name)  # refuses a linked or foreign folder first
    if not os.path.lexists(path):
        return False
    _check_target(path)
    os.remove(path)
    return True


def inside(path: str, folder: str) -> bool:
    """Whether ``path`` resolves (symlinks included) inside ``folder``."""
    resolved = os.path.normcase(os.path.realpath(path))
    root = os.path.normcase(os.path.realpath(folder))
    return resolved.startswith(root.rstrip(os.sep) + os.sep)
