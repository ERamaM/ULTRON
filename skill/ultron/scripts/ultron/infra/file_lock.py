"""Cross-process shared and exclusive locks backed by an empty SQLite file."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_file_lock(path: Path, *, exclusive: bool, timeout: float = 30) -> sqlite3.Connection:
    """Acquire a lock on a dedicated SQLite file that has not been retired by uninstall."""
    lock = sqlite3.connect(path, timeout=timeout)
    try:
        if exclusive:
            lock.execute("BEGIN EXCLUSIVE")
        else:
            lock.execute("BEGIN")
        # This read also acquires the shared lock for a deferred transaction.
        if lock.execute("PRAGMA user_version").fetchone()[0] == 1:
            raise sqlite3.OperationalError("file lock is retired; finish uninstall before retrying")
    except BaseException:
        lock.close()
        raise
    return lock
