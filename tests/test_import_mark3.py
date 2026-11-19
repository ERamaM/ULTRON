import json
import os
import sqlite3
from contextlib import closing

import pytest
import yaml

from ultron import home, import_mark3, profiles, seeds
from ultron.ledger import connect

PROFILE_SCHEMA = """CREATE TABLE search_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT UNIQUE, name TEXT, description TEXT DEFAULT '',
    prompt TEXT DEFAULT '', criteria TEXT DEFAULT '{}', query_templates TEXT DEFAULT '{}',
    source_cv_profile TEXT DEFAULT '', source_kind TEXT DEFAULT 'manual', enabled INTEGER DEFAULT 1,
    created_at TEXT%s)"""
JOBS_SCHEMA = """CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, identity_key TEXT NOT NULL UNIQUE, canonical_url TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '', company TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
    source_type TEXT NOT NULL DEFAULT '', source_key TEXT NOT NULL DEFAULT '',
    first_seen_run_id TEXT NOT NULL, first_seen_at TEXT NOT NULL, last_seen_run_id TEXT NOT NULL,
    last_seen_at TEXT NOT NULL, times_seen INTEGER NOT NULL DEFAULT 1,
    review_state TEXT NOT NULL DEFAULT 'unreviewed', review_changed_at TEXT, notes TEXT NOT NULL DEFAULT '',
    match_score INTEGER, match_band TEXT DEFAULT '', score_version TEXT DEFAULT '')"""
FEEDBACK_SCHEMA = """CREATE TABLE job_feedback (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id), relevance TEXT NOT NULL,
    reason_code TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', labeled_at TEXT NOT NULL,
    label_version INTEGER NOT NULL DEFAULT 1)"""


def make_mark3(folder, *, old=False, focus=("machine learning engineer",), source_cv=""):
    folder.mkdir(parents=True, exist_ok=True)
    db = folder / "mark_iii.sqlite3"
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(PROFILE_SCHEMA % ("" if old else ", worked_examples TEXT DEFAULT '[]'"))
        conn.execute(JOBS_SCHEMA)
        if not old:
            conn.execute(FEEDBACK_SCHEMA)
        crit = {"role_focus_terms": list(focus), "target_titles": ["ML Engineer"]}
        conn.execute(
            "INSERT INTO search_profiles (slug, name, criteria, query_templates, created_at) VALUES (?,?,?,?,?)",
            ("ml-eu", "ML EU", json.dumps(crit), json.dumps({"web": ["{title} remote"]}), "2025-01-01"),
        )
        if source_cv:
            conn.execute("UPDATE search_profiles SET source_cv_profile = ?", (source_cv,))
        if not old:
            conn.execute("UPDATE search_profiles SET worked_examples = ?", (json.dumps([{"a": 1}]),))
        for i in (1, 2):
            conn.execute(
                "INSERT INTO jobs (identity_key, canonical_url, title, company, first_seen_run_id, first_seen_at,"
                " last_seen_run_id, last_seen_at, times_seen, review_state, match_score, match_band)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"k{i}", f"https://x.test/{i}", f"Job {i}", "Acme", "r1", "2025-01-01", "r2", "2025-02-02",
                 3, "shortlisted", 80, "strong"),
            )
        if not old:
            conn.execute("INSERT INTO job_feedback (job_id, relevance, reason_code, note, labeled_at)"
                         " VALUES (1,'relevant','good','nice','2025-03-03')")
    rows = [
        {"company": "Acme", "ats": "greenhouse", "board_identifier": "acme", "profiles": ["ml-eu", "other"],
         "source_type": "agent_generated", "verification": {"postings": 7}},
        {"company": "Beta", "ats": "lever", "board_identifier": "beta", "profiles": ["ml-eu"]},
    ]
    (folder / "ats_auto_seeds.yaml").write_text(yaml.safe_dump({"seeds": rows}), encoding="utf-8")
    return db


def test_import_profiles_jobs_feedback_seeds(tmp_path):
    src = tmp_path / "m3"
    make_mark3(src)
    res = import_mark3.import_mark3(src)
    assert res["profiles"]["imported"] == 1
    prof = profiles.load_profile("ml-eu")
    assert prof["query_templates"] == {"web": ["{title} remote"]}
    assert prof["worked_examples"] == [{"a": 1}]
    assert res["seeds"]["imported"] == 2
    inv = {s.board_identifier: s for s in seeds._read_yaml_seeds(seeds.auto_seeds_path())}
    assert inv["acme"].profiles == ["ml-eu", "other"]
    assert inv["acme"].source_type == "agent_generated"
    assert inv["acme"].verification == {"postings": 7}
    assert res["jobs"] == {"imported": 2, "skipped": 0}
    assert res["feedback"] == {"imported": 1, "skipped": 0}
    assert res["missing_tables"] == []
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT first_seen, last_seen, times_seen, review_state, match_score, profile_slug,"
            " last_seen_run_id FROM jobs WHERE identity_key='k1'").fetchone()
        assert row == ("2025-01-01", "2025-02-02", 3, "shortlisted", 80, "", "r2")
        fb = conn.execute("SELECT identity_key, relevance, reason_code FROM job_feedback").fetchall()
        assert fb == [("k1", "relevant", "good")]


def test_import_twice_no_duplicates(tmp_path):
    src = tmp_path / "m3"
    make_mark3(src)
    import_mark3.import_mark3(src)
    res = import_mark3.import_mark3(src)
    assert res["profiles"]["imported"] == 0 and res["profiles"]["skipped"] == 1
    assert res["seeds"] == {"imported": 0, "skipped": 2}
    assert res["jobs"] == {"imported": 0, "skipped": 2}
    assert res["feedback"] == {"imported": 0, "skipped": 1}
    assert len(seeds._read_yaml_seeds(seeds.auto_seeds_path())) == 2
    with closing(connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2


def test_old_schema_missing_tables(tmp_path):
    src = tmp_path / "m3"
    make_mark3(src, old=True)
    res = import_mark3.import_mark3(src)
    assert "job_feedback" in res["missing_tables"]
    assert "search_profiles.worked_examples" in res["missing_tables"]
    assert res["jobs"]["imported"] == 2 and res["feedback"]["imported"] == 0
    assert "worked_examples" not in profiles.load_profile("ml-eu")


def test_source_opened_read_only(tmp_path):
    src = tmp_path / "m3"
    db = make_mark3(src)
    os.utime(db, (1_000_000_000, 1_000_000_000))
    before = {p.name: p.stat().st_mtime_ns for p in src.iterdir()}
    import_mark3.import_mark3(src)
    import_mark3.import_mark3(src, dry_run=True)
    assert {p.name: p.stat().st_mtime_ns for p in src.iterdir()} == before


def test_ai_profile_gets_vocabulary_pack(tmp_path):
    make_mark3(tmp_path / "ai")
    import_mark3.import_mark3(tmp_path / "ai")
    assert profiles.load_profile("ml-eu")["criteria"]["vocabulary_packs"] == ["ai-ml"]


def test_non_ai_profile_has_no_pack(tmp_path):
    make_mark3(tmp_path / "nonai", focus=("accountant",))
    import_mark3.import_mark3(tmp_path / "nonai")
    assert "vocabulary_packs" not in profiles.load_profile("ml-eu")["criteria"]


def test_dry_run_writes_nothing_and_checkout_uses_data(tmp_path):
    make_mark3(tmp_path / "checkout" / "data")
    res = import_mark3.import_mark3(tmp_path / "checkout", dry_run=True)
    assert res["profiles"]["imported"] == 1 and res["jobs"]["imported"] == 2 and res["seeds"]["imported"] == 2
    assert not (home.ultron_home() / "ultron.sqlite3").exists()
    assert not list((home.ultron_home() / "profiles").glob("*.yaml"))
    assert not seeds.auto_seeds_path().exists()


def test_no_database_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        import_mark3.import_mark3(tmp_path)


def test_cli_import_mark3(tmp_path, capsys):
    from ultron.cli import main

    make_mark3(tmp_path / "m3")
    assert main(["import-mark3", str(tmp_path / "m3"), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"]["imported"] == 2
    assert main(["import-mark3", str(tmp_path / "missing")]) != 0
    assert json.loads(capsys.readouterr().out)["error"] == "mark3_not_found"


def test_wal_source_left_untouched(tmp_path):
    src = tmp_path / "wal"
    db = make_mark3(src)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("UPDATE jobs SET title = 'wal row' WHERE identity_key = 'k1'")
        conn.commit()
    for p in src.glob("*-shm"):
        p.unlink(missing_ok=True)
    for p in src.glob("*-wal"):
        p.unlink(missing_ok=True)
    os.utime(db, (1_000_000_000, 1_000_000_000))

    def snapshot():
        return {p.name: (p.stat().st_mtime_ns, p.stat().st_size) for p in src.iterdir()}

    before = snapshot()
    import_mark3.import_mark3(src, dry_run=True)
    import_mark3.import_mark3(src)
    assert snapshot() == before
    assert not list(src.glob("*-shm")) and not list(src.glob("*-wal"))


@pytest.mark.parametrize("focus", ["senior ML engineer", "llm", "applied ml engineer"])
def test_ai_detection_is_not_exact_match(tmp_path, focus):
    make_mark3(tmp_path / "m3", focus=(focus,))
    res = import_mark3.import_mark3(tmp_path / "m3")
    assert res["profiles"]["vocabulary_pack_added"] == ["ml-eu"]
    assert profiles.load_profile("ml-eu")["criteria"]["vocabulary_packs"] == ["ai-ml"]


def test_finance_profile_stays_without_pack(tmp_path):
    make_mark3(tmp_path / "m3", focus=("financial controller", "accounting"))
    res = import_mark3.import_mark3(tmp_path / "m3")
    assert res["profiles"]["vocabulary_pack_added"] == []
    assert "vocabulary_packs" not in profiles.load_profile("ml-eu")["criteria"]


def test_source_cv_profile_maps_to_source_cv(tmp_path):
    make_mark3(tmp_path / "m3", source_cv="C:/cvs/me.pdf")
    import_mark3.import_mark3(tmp_path / "m3")
    prof = profiles.load_profile("ml-eu")
    assert prof["source_cv"] == "C:/cvs/me.pdf"
    assert "source_cv_profile" not in prof


def test_empty_source_cv_profile_is_omitted(tmp_path):
    make_mark3(tmp_path / "m3")
    import_mark3.import_mark3(tmp_path / "m3")
    assert "source_cv" not in profiles.load_profile("ml-eu")
