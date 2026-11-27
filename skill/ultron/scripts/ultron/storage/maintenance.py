"""Data maintenance: status, backup, check, restore, cleanup, delete-run, purge.

Safety rules (every function):
- Destructive operations are previews unless the caller passes ``execute``/``yes``.
- Every path that gets deleted or replaced is resolved and must lie inside ``ultron_home()``
  (``delete_run``/``cleanup`` additionally inside ``runs/``); symlinks that escape are refused.
- ``venv/`` and ``profiles/`` are never touched; ``backups/`` only by ``purge(include_backups=True)``.
- SQLite connections are always closed before files are deleted or replaced (Windows file locks).
- ``cleanup``/``delete_run`` remove the run folder and its ``runs`` row only; ``jobs`` and
  ``job_occurrences`` (the job history) are kept.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ultron.infra import home
from ultron.infra.file_lock import open_file_lock
from ultron.infra.fs import is_link as _is_link
from ultron.storage import ledger
from ultron.storage.runs import RUN_ID_RE

KEEP_AUTO_BACKUPS = 5
OPEN_RUN_HOURS = 24  # an unfinished run younger than this blocks purge (unless force)
_AUTO_BACKUP_RE = re.compile(r"^ultron-\d{8}T\d{6}\d*\.sqlite3$")  # excludes "-pre-v2"/"-pre-restore" copies
_DB_SIDECARS = ("", "-wal", "-shm", "-journal")
HOME_MARKER = ".ultron-home"  # written by install.py into a home it created


class MaintenanceError(Exception):
    """Raised on invalid maintenance arguments or storage consistency failures."""
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


# ---------------------------------------------------------------- helpers

def _root() -> Path:
    return home.ultron_home().resolve()


def _db_path() -> Path:
    return home.ultron_home() / "ultron.sqlite3"


def _contained(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _safe_target(path: Path, root: Path, *, link_ok: bool = False) -> Path:
    """Return ``path`` if it is strictly inside ``root`` after resolving symlinks, else raise.

    With ``link_ok`` a symlink/junction is acceptable when its *parent* is inside ``root``: removing the
    link itself never touches what it points to.
    """
    if link_ok and _is_link(path):
        parent = path.parent.resolve()
        if parent != root.resolve() and not _contained(parent, root):
            raise MaintenanceError("unsafe_path", f"refusing to touch a path outside {root}: {path}")
        return path
    resolved = path.resolve()
    if resolved == root.resolve() or not _contained(resolved, root):
        raise MaintenanceError("unsafe_path", f"refusing to touch a path outside {root}: {path}")
    return path


def _remove(path: Path, root: Path, *, link_ok: bool = False) -> None:
    _safe_target(path, root, link_ok=link_ok)
    if path.is_symlink():
        path.unlink(missing_ok=True)
    elif _is_link(path):
        os.rmdir(path)  # Windows junction: removes the junction only
    elif path.is_file():
        path.unlink(missing_ok=True)
    elif path.is_dir():
        shutil.rmtree(path)


def _runs_root_checked() -> Path:
    runs_root = home.ultron_home() / "runs"
    if runs_root.exists() and not _contained(runs_root, _root()):
        raise MaintenanceError("unsafe_path", f"runs/ resolves outside {_root()}")
    return runs_root


def _read_run(path: Path) -> dict | None:
    try:
        return json.loads((path / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _created(data: dict) -> datetime | None:
    try:
        value = datetime.fromisoformat(data["created_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _run_folders() -> list[Path]:
    runs_root = home.ultron_home() / "runs"
    return sorted(p for p in runs_root.iterdir() if p.is_dir() or p.is_symlink()) if runs_root.is_dir() else []


def _delete_run_row(run_id: str) -> None:
    if not _db_path().exists():
        return
    with closing(ledger.connect()) as conn, conn:
        conn.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))


# ---------------------------------------------------------------- status / check

def status() -> dict:
    """Report ULTRON home paths, SQLite integrity, sizes, and item counts."""
    root = home.ultron_home()
    db = _db_path()
    out: dict = {
        "home": str(root),
        "database": str(db),
        "database_exists": db.exists(),
        "database_bytes": db.stat().st_size if db.exists() else 0,
        "schema_version": None,
        "jobs": 0,
        "runs_in_db": 0,
        "runs_on_disk": len([p for p in _run_folders() if RUN_ID_RE.fullmatch(p.name)]),
        "backups": len(list((root / "backups").glob("*.sqlite3"))) if (root / "backups").is_dir() else 0,
    }
    if db.exists():
        try:
            with closing(_readonly(db)) as conn:
                out["schema_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
                out["jobs"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                out["runs_in_db"] = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            out["database_error"] = str(exc)
    return out


def check() -> dict:
    """Run SQLite integrity and run-folder consistency diagnostics."""
    db = _db_path()
    integrity = "missing"
    in_db: set[str] = set()
    if db.exists():
        try:
            with closing(_readonly(db)) as conn:
                integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity == "ok":
                    in_db = {r[0] for r in conn.execute("SELECT run_id FROM runs")}
        except sqlite3.DatabaseError as exc:
            integrity = f"error: {exc}"
    on_disk = {p.name for p in _run_folders()}
    no_folder = sorted(in_db - on_disk)
    no_row = sorted(n for n in on_disk - in_db if RUN_ID_RE.fullmatch(n))
    return {
        "ok": integrity in ("ok", "missing") and not no_folder and not no_row,
        "integrity": integrity,
        "runs_in_db_without_folder": no_folder,
        "runs_on_disk_without_row": no_row,
    }


# ---------------------------------------------------------------- backup / restore

def _copy_db(source: sqlite3.Connection, target: Path) -> None:
    with closing(sqlite3.connect(target)) as dest:
        source.backup(dest)


def backup() -> Path:
    """Snapshot the ledger to ``backups/ultron-<timestamp>.sqlite3``; keep the 5 newest automatic ones."""
    if not _db_path().exists():
        raise MaintenanceError("not_found", "no database to back up")
    folder = home.ultron_home() / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target = folder / f"ultron-{stamp}.sqlite3"
    with closing(ledger.connect()) as conn:
        _copy_db(conn, target)
    for old in sorted(p for p in folder.iterdir() if _AUTO_BACKUP_RE.match(p.name))[:-KEEP_AUTO_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def _verify_candidate(path: Path) -> int:
    """Raise unless ``path`` is a sound ULTRON ledger; returns its schema version."""
    if not path.is_file():
        raise MaintenanceError("not_found", f"backup file not found: {path}")
    try:
        with closing(_readonly(path)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise MaintenanceError("corrupt_backup", f"integrity_check failed: {result}")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "jobs" not in tables:
                raise MaintenanceError("not_ultron_database", "file has no `jobs` table")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.DatabaseError as exc:
        raise MaintenanceError("corrupt_backup", str(exc)) from exc
    if version > ledger.SCHEMA_VERSION:
        raise MaintenanceError("newer_schema", f"backup schema v{version} is newer than this ULTRON (v{ledger.SCHEMA_VERSION})")
    return version


def restore(path: Path, *, yes: bool) -> dict:
    """Restore the SQLite ledger from a verified backup after backing up current state."""
    path = Path(path)
    version = _verify_candidate(path)
    root = _root()
    db = _db_path()
    if path.resolve() == db.resolve():
        raise MaintenanceError("unsafe_path", "cannot restore the live database onto itself")
    if not yes:
        return {"preview": True, "would_replace": str(db), "from": str(path), "schema_version": version}
    _safe_target(db, root)
    lock = _safe_target(ledger.lock_path(db), root)
    try:
        lease = open_file_lock(lock, exclusive=True, timeout=5)
    except sqlite3.OperationalError as exc:
        raise MaintenanceError("ledger_busy", "close concurrent ledger operations and retry the restore") from exc
    with closing(lease):
        previous = _restore_locked(path, db, root)
    return {"preview": False, "restored": str(db), "from": str(path), "previous_backup": str(previous) if previous else None}


def _restore_locked(path: Path, db: Path, root: Path) -> Path | None:
    previous = None
    corrupt = False
    if db.exists():
        folder = root / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        previous = folder / f"ultron-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%f')}-pre-restore.sqlite3"
        try:
            with closing(_readonly(db)) as conn:
                _copy_db(conn, previous)
        except sqlite3.DatabaseError as exc:
            if exc.sqlite_errorcode not in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB}:
                raise
            shutil.copy2(db, previous)  # current DB unreadable: keep the raw bytes
            corrupt = True
    fd, tmp = tempfile.mkstemp(dir=root, prefix="restore-", suffix=".tmp")
    os.close(fd)
    try:
        with closing(_readonly(path)) as src:
            _copy_db(src, Path(tmp))
        if corrupt or not db.exists():
            if any(Path(str(db) + suffix).exists() for suffix in _DB_SIDECARS[1:]):
                raise MaintenanceError("ledger_busy", "ledger recovery files are present")
            os.replace(tmp, db)
        else:
            # SQLite publishes into the same file even when another connection is reading it.
            with closing(_readonly(Path(tmp))) as src:
                _copy_db(src, db)
    finally:
        Path(tmp).unlink(missing_ok=True)
    return previous


# ---------------------------------------------------------------- run deletion

def _delete_folders(run_ids: list[str], execute: bool) -> dict:
    runs_root = _runs_root_checked()
    if not execute:
        return {"preview": True, "would_delete": run_ids}
    deleted = []
    for rid in run_ids:
        _remove(runs_root / rid, runs_root)
        _delete_run_row(rid)
        deleted.append(rid)
    return {"preview": False, "deleted": deleted}


def cleanup(days: int, *, execute: bool) -> dict:
    """Identify or delete old run directories older than ``days`` while keeping ledger data."""
    if days < 0:
        raise MaintenanceError("invalid_value", "days must be >= 0")
    cutoff = datetime.now(UTC) - timedelta(days=days)
    _runs_root_checked()
    victims = []
    for folder in _run_folders():
        if folder.is_symlink() or not RUN_ID_RE.fullmatch(folder.name):
            continue
        data = _read_run(folder)
        created = _created(data) if data else None
        if data and created and "finalize" in data.get("stages_done", []) and created < cutoff:
            victims.append(folder.name)
    out = _delete_folders(victims, execute)
    out["older_than_days"] = days
    out["note"] = "job history (jobs, job_occurrences) is kept"
    return out


def delete_run(run_id: str, *, yes: bool) -> dict:
    """Remove a single run directory identified by ``run_id`` when confirmed."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise MaintenanceError("invalid_run_id", f"run id must look like YYYYMMDD-HHMMSS-xxxxxx: {run_id!r}")
    runs_root = _runs_root_checked()
    target = runs_root / run_id
    _safe_target(target, runs_root)
    if not target.exists() and not target.is_symlink():
        raise MaintenanceError("not_found", f"run not found: {run_id}")
    out = _delete_folders([run_id], yes)
    out["note"] = "job history (jobs, job_occurrences) is kept"
    return out


# ---------------------------------------------------------------- purge

def _open_runs() -> list[str]:
    cutoff = datetime.now(UTC) - timedelta(hours=OPEN_RUN_HOURS)
    found = []
    for folder in _run_folders():
        data = _read_run(folder)
        if not data or "finalize" in data.get("stages_done", []):
            continue
        created = _created(data)
        if created is None or created > cutoff:
            found.append(folder.name)
    return found


def purge(*, yes: bool, include_backups: bool = False, force: bool = False) -> dict:
    """Purge local ULTRON run, profile, seed, CV, and ledger data when confirmed."""
    root = _root()
    open_runs = _open_runs()
    if open_runs and not force:
        raise MaintenanceError("open_run", f"recent unfinished runs exist ({', '.join(open_runs[:3])}); finish them or pass --force")
    home_dir = home.ultron_home()
    # Generic names (cv/, backups/, anything in runs/) are only ULTRON's in a home install.py marked as its own.
    # The ledger is no proof: the CLI creates it on first use in whatever folder ULTRON_HOME points at.
    owned = (home_dir / HOME_MARKER).is_file()
    targets = [home_dir / f"ultron.sqlite3{s}" for s in _DB_SIDECARS]
    targets += [home_dir / "runs", home_dir / "seeds" / "auto_seeds.yaml"]
    if owned:
        targets.append(home_dir / "cv")
        if include_backups:
            targets.append(home_dir / "backups")
    present = [t for t in targets if t.exists() or _is_link(t)]
    plan: list[Path] = []
    for t in present:
        if t.name == "runs" and t.is_dir() and not _is_link(t):
            # keep the (empty) runs/ folder itself; without the marker only ULTRON run folders go
            plan.extend(c for c in sorted(t.iterdir()) if owned or RUN_ID_RE.fullmatch(c.name))
        elif t.name != "runs" or owned:
            plan.append(t)
    for t in plan:  # validate everything BEFORE deleting anything
        _safe_target(t, root, link_ok=True)
    if not yes:
        return {
            "preview": True,
            "would_delete": [str(t) for t in plan],
            "keeps": ["profiles", "venv"] + ([] if include_backups and owned else ["backups"])
            + ([] if owned else ["cv", "runs/ entries that are not ULTRON run folders"]),
        }
    for t in plan:
        _remove(t, root, link_ok=True)
    return {"preview": False, "deleted": [str(t) for t in plan]}
