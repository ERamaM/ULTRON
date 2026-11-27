"""Cross-process shared and exclusive locks backed by an empty SQLite file."""

from __future__ import annotations

import sqlite3
from pathlib import Path


def open_file_lock(path: Path, *, exclusive: bool, timeout: float = 30) -> sqlite3.Connection:
    """Acquire a lock on a dedicated SQLite file and hold it until closed."""
    lock = sqlite3.connect(path, timeout=timeout)
    try:
        if exclusive:
            lock.execute("BEGIN EXCLUSIVE")
        else:
            lock.execute("BEGIN")
            lock.execute("SELECT name FROM sqlite_schema LIMIT 1").fetchone()
    except BaseException:
        lock.close()
        raise
    return lock
