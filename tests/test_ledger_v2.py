import sqlite3
from contextlib import closing

import pytest

from ultron import home, ledger

V1 = """
CREATE TABLE jobs (identity_key TEXT PRIMARY KEY, canonical_url TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
 company TEXT NOT NULL DEFAULT '', first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
 times_seen INTEGER NOT NULL DEFAULT 0, review_state TEXT NOT NULL DEFAULT 'unreviewed');
CREATE TABLE job_occurrences (identity_key TEXT NOT NULL, run_id TEXT NOT NULL, PRIMARY KEY (identity_key, run_id));
CREATE TABLE runs (run_id TEXT PRIMARY KEY, profile_slug TEXT NOT NULL DEFAULT '', preset TEXT NOT NULL DEFAULT '',
 created_at TEXT, finished_at TEXT, roles_kept INTEGER NOT NULL DEFAULT 0,
 discard_counts_json TEXT NOT NULL DEFAULT '{}');
"""


def _make_v1():
    path = home.ultron_home() / "ultron.sqlite3"
    with closing(sqlite3.connect(path)) as c:
        c.executescript(V1)
        for i in range(3):
            c.execute("INSERT INTO jobs VALUES (?,?,?,?,?,?,2,'shortlisted')",
                      (f"k{i}", f"https://x/{i}", f"T{i}", "Co", "2026-01-01", "2026-02-01"))
            c.execute("INSERT INTO job_occurrences VALUES (?, 'r1')", (f"k{i}",))
        c.execute("PRAGMA user_version = 1")
        c.commit()


def test_migrate_v1_to_current_preserves_rows():
    _make_v1()
    with closing(ledger.connect()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
        rows = conn.execute("SELECT identity_key, times_seen, review_state, first_seen_run_id FROM jobs").fetchall()
        assert len(rows) == 3
        assert {r[1] for r in rows} == {2}
        assert {r[2] for r in rows} == {"shortlisted"}
        assert {r[3] for r in rows} == {"r1"}
        for table in ("job_feedback", "saved_searches", "diagnostics"):
            conn.execute(f"SELECT * FROM {table}")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE jobs SET review_state = 'maybe'")
    backups = list((home.ultron_home() / "backups").glob("ultron-*-pre-v2.sqlite3"))
    assert len(backups) == 1
    with closing(sqlite3.connect(backups[0])) as b:
        assert b.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 3
        assert b.execute("PRAGMA user_version").fetchone()[0] == 1


def test_connect_is_idempotent_and_fresh_has_no_backup():
    with closing(ledger.connect()) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == ledger.SCHEMA_VERSION
    with closing(ledger.connect()) as conn:
        assert ledger.migrate(conn) == ledger.SCHEMA_VERSION
    assert not (home.ultron_home() / "backups").exists()


def _stale_first_read(monkeypatch, stale: int):
    real = ledger._version
    calls = {"n": 0}

    def fake(conn):
        calls["n"] += 1
        return stale if calls["n"] == 1 else real(conn)

    monkeypatch.setattr(ledger, "_version", fake)


def test_migrate_rechecks_version_under_the_lock(monkeypatch):
    # R5: another process migrated between our lock-free read and the write lock -> no rebuild.
    _make_v1()
    path = home.ultron_home() / "ultron.sqlite3"
    with closing(sqlite3.connect(path)) as first, closing(sqlite3.connect(path)) as second:
        assert ledger.migrate(second) == ledger.SCHEMA_VERSION
        second.execute("UPDATE jobs SET match_score = 77, notes = 'kept'")
        second.commit()
        _stale_first_read(monkeypatch, 1)
        assert ledger.migrate(first) == ledger.SCHEMA_VERSION
        assert first.execute("SELECT match_score, notes FROM jobs WHERE identity_key = 'k0'").fetchone() == (77, "kept")


def test_fresh_create_rechecks_version_under_the_lock(monkeypatch):
    path = home.ultron_home() / "ultron.sqlite3"
    with closing(sqlite3.connect(path)) as first, closing(sqlite3.connect(path)) as second:
        assert ledger.migrate(second) == ledger.SCHEMA_VERSION
        second.execute("INSERT INTO jobs (identity_key, canonical_url, first_seen, last_seen, match_score)"
                       " VALUES ('k', 'u', 'a', 'b', 5)")
        second.commit()
        _stale_first_read(monkeypatch, 0)
        assert ledger.migrate(first) == ledger.SCHEMA_VERSION
        assert first.execute("SELECT match_score FROM jobs").fetchall() == [(5,)]
    assert not (home.ultron_home() / "backups").exists()


def test_migrate_v2_backfills_novelty_and_backs_up():
    _make_v1()
    path = home.ultron_home() / "ultron.sqlite3"
    with closing(sqlite3.connect(path)) as conn, conn:
        ledger._apply_v2(conn)
        conn.execute("INSERT INTO job_occurrences (identity_key, run_id) VALUES ('k0', 'r2')")
        conn.execute("UPDATE jobs SET last_seen_run_id = 'r2' WHERE identity_key = 'k0'")
        conn.execute("PRAGMA user_version = 2")

    with closing(ledger.connect()) as conn:
        assert conn.execute("""
            SELECT run_id, is_new, details_json IS NOT NULL FROM job_occurrences
            WHERE identity_key = 'k0' ORDER BY rowid
        """).fetchall() == [("r1", 1, 0), ("r2", 0, 1)]
    [backup] = (home.ultron_home() / "backups").glob("ultron-*-pre-v3.sqlite3")
    with closing(sqlite3.connect(backup)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (2,)
