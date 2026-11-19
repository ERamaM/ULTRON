"""Minimal job history (SQLite) at ``$ULTRON_HOME/ultron.sqlite3``."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from ultron import home
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import FinalRow, RunSummary

SCHEMA_VERSION = 3

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    identity_key TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 0,
    review_state TEXT NOT NULL DEFAULT 'unreviewed'
);
CREATE TABLE IF NOT EXISTS job_occurrences (
    identity_key TEXT NOT NULL,
    run_id TEXT NOT NULL,
    PRIMARY KEY (identity_key, run_id)
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    profile_slug TEXT NOT NULL DEFAULT '',
    preset TEXT NOT NULL DEFAULT '',
    created_at TEXT,
    finished_at TEXT,
    roles_kept INTEGER NOT NULL DEFAULT 0,
    discard_counts_json TEXT NOT NULL DEFAULT '{}'
);
"""


_JOBS_V2 = """
CREATE TABLE jobs_v2 (
    identity_key TEXT PRIMARY KEY,
    canonical_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    times_seen INTEGER NOT NULL DEFAULT 0,
    review_state TEXT NOT NULL DEFAULT 'unreviewed'
        CHECK (review_state IN ('unreviewed', 'shortlisted', 'applied', 'rejected', 'archived')),
    location TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '',
    source_key TEXT NOT NULL DEFAULT '',
    first_seen_run_id TEXT,
    last_seen_run_id TEXT,
    review_changed_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    match_score INTEGER,
    match_band TEXT,
    profile_slug TEXT NOT NULL DEFAULT ''
)
"""

_V2_TABLES = (
    """CREATE TABLE IF NOT EXISTS job_feedback (
    identity_key TEXT PRIMARY KEY REFERENCES jobs(identity_key),
    relevance TEXT NOT NULL CHECK (relevance IN ('relevant', 'not_relevant', 'uncertain')),
    reason_code TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    labeled_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS saved_searches (
    name TEXT PRIMARY KEY,
    profile_slug TEXT NOT NULL DEFAULT '',
    preset TEXT NOT NULL DEFAULT '',
    prompt TEXT NOT NULL DEFAULT '',
    overrides_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)""",
    """CREATE TABLE IF NOT EXISTS diagnostics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitted_url TEXT NOT NULL DEFAULT '',
    identity_key TEXT NOT NULL DEFAULT '',
    first_failed_stage TEXT NOT NULL DEFAULT '',
    reason_code TEXT NOT NULL DEFAULT '',
    trace_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
)""",
)

_V1_COLUMNS = "identity_key, canonical_url, title, company, first_seen, last_seen, times_seen, review_state"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _backup(conn: sqlite3.Connection, target_version: int) -> Path:
    """Copy the ledger to ``backups/`` and verify the copy; raises if it is not sound."""
    folder = home.ultron_home() / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    target = folder / f"ultron-{stamp}-pre-v{target_version}.sqlite3"
    expected = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    with closing(sqlite3.connect(target)) as dest:
        conn.backup(dest)
    with closing(sqlite3.connect(target)) as check:
        ok = check.execute("PRAGMA integrity_check").fetchone()[0]
        count = check.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    if ok != "ok" or count != expected:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"ledger backup verification failed ({ok}, {count} != {expected} jobs)")
    return target


def _run_pick(order: str) -> str:
    return (
        "(SELECT o.run_id FROM job_occurrences o LEFT JOIN runs r ON r.run_id = o.run_id"
        f" WHERE o.identity_key = jobs.identity_key ORDER BY r.created_at {order}, o.run_id {order} LIMIT 1)"
    )


def _create_v1(conn: sqlite3.Connection) -> None:
    """Create the v1 tables inside the caller's transaction (``executescript`` would commit)."""
    for statement in _SCHEMA_V1.split(";"):
        if statement.strip():
            conn.execute(statement)


def _apply_v2(conn: sqlite3.Connection) -> None:
    """Rebuild ``jobs`` with the v2 columns/CHECK and add the new tables (inside the caller's transaction)."""
    conn.execute("DROP TABLE IF EXISTS jobs_v2")
    conn.execute(_JOBS_V2)
    conn.execute(
        f"INSERT INTO jobs_v2 ({_V1_COLUMNS}, first_seen_run_id, last_seen_run_id)"
        f" SELECT {_V1_COLUMNS}, {_run_pick('ASC')}, {_run_pick('DESC')} FROM jobs"
    )
    conn.execute("DROP TABLE jobs")
    conn.execute("ALTER TABLE jobs_v2 RENAME TO jobs")
    for statement in _V2_TABLES:
        conn.execute(statement)


def _apply_v3(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE job_occurrences ADD COLUMN is_new INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE job_occurrences ADD COLUMN seen_at TEXT")
    conn.execute("ALTER TABLE job_occurrences ADD COLUMN details_json TEXT")
    conn.execute("CREATE INDEX job_occurrences_run ON job_occurrences (run_id)")
    conn.execute(
        "UPDATE job_occurrences AS o SET is_new = rowid = (SELECT MIN(prior.rowid)"
        " FROM job_occurrences prior WHERE prior.identity_key = o.identity_key),"
        " seen_at = COALESCE((SELECT CASE WHEN j.first_seen_run_id = o.run_id THEN j.first_seen"
        " WHEN j.last_seen_run_id = o.run_id THEN j.last_seen END FROM jobs j WHERE j.identity_key = o.identity_key),"
        " (SELECT finished_at FROM runs WHERE run_id = o.run_id))"
    )
    columns = ", ".join(f"j.{column}" for column in _JOB_DETAILS)
    conn.execute(
        f"UPDATE job_occurrences AS o SET details_json = (SELECT json_array({columns}) FROM jobs j"
        " WHERE j.identity_key = o.identity_key AND j.last_seen_run_id = o.run_id)"
    )


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the ledger up to ``SCHEMA_VERSION`` (forward only); backs up first. Returns the final version.

    Creation and every upgrade run in one ``BEGIN IMMEDIATE`` transaction; the version is re-read after the
    write lock is taken, so a concurrent process that migrated first turns this call into a no-op (R5).
    """
    conn.commit()
    version = _version(conn)
    if version >= SCHEMA_VERSION:
        return version  # lock-free fast path
    backed_up = set()
    while True:
        if 0 < version < SCHEMA_VERSION and version not in backed_up:
            _backup(conn, version + 1)  # outside the lock; a spare backup after a lost race is harmless
            backed_up.add(version)
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = _version(conn)
            if 0 < version < SCHEMA_VERSION and version not in backed_up:
                conn.execute("ROLLBACK")
                continue
            if version == 0:
                _create_v1(conn)
                version = 1
            if version == 1:
                _apply_v2(conn)
                version = 2
            if version == 2:
                _apply_v3(conn)
                version = 3
                conn.execute(f"PRAGMA user_version = {version}")
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        return version


def connect() -> sqlite3.Connection:
    """Open (and create/migrate if needed) the ledger. Callers must close it (``contextlib.closing``)."""
    conn = sqlite3.connect(home.ultron_home() / "ultron.sqlite3")
    try:
        migrate(conn)
    except BaseException:
        conn.close()
        raise
    return conn


_JOB_DETAILS = (
    "canonical_url", "title", "company", "location", "source_type", "source_key",
    "match_score", "match_band", "profile_slug",
)
_SET_DETAILS = ", ".join(f"{column} = ?" for column in _JOB_DETAILS)


def _remove_occurrence(conn: sqlite3.Connection, run_id: str, key: str) -> None:
    conn.execute("DELETE FROM job_occurrences WHERE run_id = ? AND identity_key = ?", (run_id, key))
    first = conn.execute(
        "SELECT run_id, seen_at FROM job_occurrences WHERE identity_key = ? ORDER BY rowid LIMIT 1",
        (key,),
    ).fetchone()
    last = conn.execute(
        "SELECT run_id, seen_at, details_json FROM job_occurrences WHERE identity_key = ? ORDER BY rowid DESC LIMIT 1",
        (key,),
    ).fetchone()
    first_run, first_seen = first or (None, None)
    last_run, last_seen, details = last or (None, None, None)
    if details is not None:
        conn.execute(
            f"UPDATE jobs SET {_SET_DETAILS} WHERE identity_key = ? AND last_seen_run_id = ?",
            (*json.loads(details), key, run_id),
        )
    conn.execute(
        "UPDATE jobs SET times_seen = MAX(0, times_seen - 1),"
        " first_seen = CASE WHEN first_seen_run_id = ? THEN COALESCE(?, first_seen) ELSE first_seen END,"
        " last_seen = CASE WHEN last_seen_run_id = ? THEN COALESCE(?, last_seen) ELSE last_seen END,"
        " first_seen_run_id = CASE WHEN first_seen_run_id = ? THEN ? ELSE first_seen_run_id END,"
        " last_seen_run_id = CASE WHEN last_seen_run_id = ? THEN ? ELSE last_seen_run_id END"
        " WHERE identity_key = ?",
        (run_id, first_seen, run_id, last_seen, run_id, first_run, run_id, last_run, key),
    )
    conn.execute(
        "DELETE FROM jobs WHERE identity_key = ? AND times_seen = 0 AND review_state = 'unreviewed'"
        " AND review_changed_at IS NULL AND notes = ''"
        " AND NOT EXISTS (SELECT 1 FROM job_feedback WHERE identity_key = ?)",
        (key, key),
    )


def _record_occurrences(
    conn: sqlite3.Connection, run_id: str, rows: list[FinalRow], profile_slug: str,
) -> dict[str, bool]:
    previous = dict(conn.execute("SELECT identity_key, is_new FROM job_occurrences WHERE run_id = ?", (run_id,)))
    result: dict[str, bool] = {}
    now = _now()
    for row in rows:
        ident = canonical_job_identity(row.apply_url, source_type=row.source_type, source_key=row.source_key)
        key = ident.identity_key
        if key in result:
            continue
        details = (
            ident.canonical_url, row.title, row.company, row.location, row.source_type, row.source_key,
            row.match_score, row.match_band, profile_slug,
        )
        snapshot = json.dumps(details, ensure_ascii=False)
        if key in previous:
            result[key] = bool(previous[key])
            conn.execute(
                "UPDATE job_occurrences SET details_json = ? WHERE identity_key = ? AND run_id = ?",
                (snapshot, key, run_id),
            )
            conn.execute(
                f"UPDATE jobs SET {_SET_DETAILS} WHERE identity_key = ? AND last_seen_run_id = ?",
                (*details, key, run_id),
            )
            continue

        result[key] = conn.execute(
            "SELECT 1 FROM job_occurrences WHERE identity_key = ? LIMIT 1", (key,),
        ).fetchone() is None
        conn.execute(
            "INSERT INTO job_occurrences (identity_key, run_id, is_new, seen_at, details_json) VALUES (?, ?, ?, ?, ?)",
            (key, run_id, result[key], now, snapshot),
        )
        columns = ", ".join(_JOB_DETAILS)
        placeholders = ", ".join("?" for _ in details)
        updates = ", ".join(f"{column} = excluded.{column}" for column in _JOB_DETAILS)
        conn.execute(
            f"INSERT INTO jobs (identity_key, first_seen, last_seen, times_seen, first_seen_run_id, last_seen_run_id, {columns})"
            f" VALUES (?, ?, ?, 1, ?, ?, {placeholders})"
            " ON CONFLICT(identity_key) DO UPDATE SET times_seen = times_seen + 1, last_seen = excluded.last_seen,"
            " first_seen = CASE WHEN jobs.times_seen = 0 THEN excluded.first_seen ELSE jobs.first_seen END,"
            " last_seen_run_id = excluded.last_seen_run_id,"
            " first_seen_run_id = COALESCE(jobs.first_seen_run_id, excluded.first_seen_run_id),"
            f" {updates}",
            (key, now, now, run_id, run_id, *details),
        )
    for key in previous.keys() - result.keys():
        _remove_occurrence(conn, run_id, key)
    return result


def record_occurrences(
    conn: sqlite3.Connection, run_id: str, rows: list[FinalRow], *, profile_slug: str = ""
) -> dict[str, bool]:
    """Replace a run's occurrences and return the novelty recorded when each was added."""
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        return _record_occurrences(conn, run_id, rows, profile_slug)


def _record_run(conn: sqlite3.Connection, run_id: str, summary: RunSummary, profile_slug: str, preset: str) -> None:
    conn.execute(
        "INSERT INTO runs (run_id, profile_slug, preset, created_at, finished_at, roles_kept, discard_counts_json)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(run_id) DO UPDATE SET profile_slug = excluded.profile_slug, preset = excluded.preset,"
        " created_at = excluded.created_at, finished_at = excluded.finished_at,"
        " roles_kept = excluded.roles_kept, discard_counts_json = excluded.discard_counts_json",
        (
            run_id, profile_slug, preset, summary.started_at.isoformat(), summary.ended_at.isoformat(),
            summary.roles_kept, json.dumps(summary.discard_counts, ensure_ascii=False),
        ),
    )


def record_run(conn: sqlite3.Connection, run_id: str, summary: RunSummary, profile_slug: str, preset: str) -> None:
    with conn:
        _record_run(conn, run_id, summary, profile_slug, preset)


def record_finalized_run(
    conn: sqlite3.Connection, summary: RunSummary, rows: list[FinalRow], *, profile_slug: str, preset: str,
) -> dict[str, bool]:
    with conn:
        _record_run(conn, summary.run_id, summary, profile_slug, preset)
        return _record_occurrences(conn, summary.run_id, rows, profile_slug)
