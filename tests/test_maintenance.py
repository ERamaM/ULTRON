from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from ultron.cli import main
from ultron.infra import home
from ultron.profiles import store as profiles
from ultron.storage import ledger, maintenance, runs


def _mk_run(finished: bool = True, age_days: float = 0) -> str:
    profiles.install_example()
    rid = runs.new_run(profiles.EXAMPLE_SLUG, "balanced")
    path = runs.run_dir(rid) / "run.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["created_at"] = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    if finished:
        data["stages_done"].append("finalize")
    path.write_text(json.dumps(data), encoding="utf-8")
    with closing(ledger.connect()) as conn, conn:
        conn.execute("INSERT INTO runs (run_id, created_at) VALUES (?, ?)", (rid, data["created_at"]))
        conn.execute(
            "INSERT OR IGNORE INTO jobs (identity_key, canonical_url, first_seen, last_seen) VALUES ('k1','u','a','b')"
        )
        conn.execute("INSERT INTO job_occurrences (identity_key, run_id) VALUES ('k1', ?)", (rid,))
    return rid


def _counts():
    with closing(ledger.connect()) as conn:
        return tuple(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("jobs", "job_occurrences", "runs"))


def test_cleanup_preview_deletes_nothing():
    old = _mk_run(age_days=40)
    out = maintenance.cleanup(30, execute=False)
    assert out["preview"] is True and old in str(out["would_delete"])
    assert runs.run_dir(old).is_dir()
    assert _counts() == (1, 1, 1)


def test_cleanup_execute_keeps_ledger():
    old = _mk_run(age_days=40)
    new = _mk_run(age_days=1)
    open_old = _mk_run(finished=False, age_days=90)
    out = maintenance.cleanup(30, execute=True)
    assert out["preview"] is False and out["deleted"] == [old]
    assert not runs.run_dir(old).exists()
    assert runs.run_dir(new).is_dir() and runs.run_dir(open_old).is_dir()
    jobs, occ, run_rows = _counts()
    assert jobs == 1 and occ == 3 and run_rows == 2


def test_delete_run_rejects_traversal(tmp_path):
    victim = home.ultron_home() / "victim"
    victim.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    for bad in ("../x", "..", "../../outside", "", "20250101-000000-ZZZZZZ", str(outside)):
        with pytest.raises(maintenance.MaintenanceError):
            maintenance.delete_run(bad, yes=True)
    assert victim.is_dir() and outside.is_dir()
    assert main(["data", "delete-run", "../x", "--yes"]) != 0


def test_delete_run_symlink_escape_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    rid = "20250101-000000-abcdef"
    link = home.ultron_home() / "runs" / rid
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.delete_run(rid, yes=True)
    assert (outside / "keep.txt").is_file()


def test_delete_run_preview_then_yes():
    rid = _mk_run()
    out = maintenance.delete_run(rid, yes=False)
    assert out["preview"] is True and runs.run_dir(rid).is_dir()
    out = maintenance.delete_run(rid, yes=True)
    assert out["preview"] is False and not runs.run_dir(rid).exists()
    assert _counts() == (1, 1, 0)


def _mark_home(root):
    """What install.py writes into a home it created: only then are generic names like cv/ ULTRON's."""
    (root / maintenance.HOME_MARKER).write_text("x", encoding="utf-8")


def _seed_home():
    root = home.ultron_home()
    _mark_home(root)
    _mk_run()
    (root / "seeds" / "auto_seeds.yaml").write_text("x: 1", encoding="utf-8")
    (root / "cv").mkdir()
    (root / "cv" / "cv.txt").write_text("cv", encoding="utf-8")
    (root / "venv").mkdir()
    (root / "venv" / "marker").write_text("v", encoding="utf-8")
    maintenance.backup()
    return root


def test_purge_keeps_backups_and_profiles():
    root = _seed_home()
    preview = maintenance.purge(yes=False)
    assert preview["preview"] is True and (root / "ultron.sqlite3").exists()
    out = maintenance.purge(yes=True)
    assert out["preview"] is False
    assert not (root / "ultron.sqlite3").exists()
    assert not list((root / "runs").iterdir())
    assert not (root / "seeds" / "auto_seeds.yaml").exists() and not (root / "cv").exists()
    assert (root / "profiles").is_dir() and list((root / "profiles").iterdir())
    assert (root / "venv" / "marker").is_file()
    assert list((root / "backups").glob("*.sqlite3"))


def test_purge_include_backups():
    root = _seed_home()
    maintenance.purge(yes=True, include_backups=True)
    assert not (root / "backups").exists()
    assert (root / "venv" / "marker").is_file()


def test_purge_refuses_open_run():
    root = _seed_home()
    _mk_run(finished=False)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.purge(yes=True)
    assert (root / "ultron.sqlite3").exists()
    out = maintenance.purge(yes=True, force=True)
    assert out["preview"] is False and not (root / "ultron.sqlite3").exists()


def test_restore_rejects_corrupt_file(tmp_path):
    _mk_run()
    bad = tmp_path / "bad.sqlite3"
    bad.write_bytes(b"this is not a database" * 100)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.restore(bad, yes=True)
    notul = tmp_path / "other.sqlite3"
    with closing(sqlite3.connect(notul)) as c, c:
        c.execute("CREATE TABLE t (a)")
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.restore(notul, yes=True)
    assert _counts()[0] == 1


def test_restore_roundtrip_backs_up_current():
    _mk_run()
    snap = maintenance.backup()
    with closing(ledger.connect()) as conn, conn:
        conn.execute("DELETE FROM jobs")
    assert _counts()[0] == 0
    preview = maintenance.restore(snap, yes=False)
    assert preview["preview"] is True and _counts()[0] == 0
    out = maintenance.restore(snap, yes=True)
    assert out["preview"] is False and out["previous_backup"]
    assert _counts()[0] == 1
    assert len(list((home.ultron_home() / "backups").glob("*.sqlite3"))) >= 2


def test_backup_rotation_keeps_5():
    _mk_run()
    pre = home.ultron_home() / "backups" / "ultron-20200101T000000000000-pre-v2.sqlite3"
    pre.parent.mkdir(exist_ok=True)
    pre.write_bytes(b"x")
    paths = [maintenance.backup() for _ in range(7)]
    auto = [p for p in (home.ultron_home() / "backups").glob("ultron-*.sqlite3") if "-pre-" not in p.name]
    assert len(auto) == 5 and paths[-1] in auto and paths[0] not in auto
    assert pre.exists()


def test_status_and_check():
    rid = _mk_run()
    st = maintenance.status()
    assert st["schema_version"] == ledger.SCHEMA_VERSION and st["runs_on_disk"] == 1 and st["jobs"] == 1
    assert st["database_bytes"] > 0
    assert maintenance.check()["ok"] is True
    import shutil

    shutil.rmtree(runs.run_dir(rid))
    (home.ultron_home() / "runs" / "20240101-000000-abcdef").mkdir()
    chk = maintenance.check()
    assert chk["integrity"] == "ok"
    assert chk["runs_in_db_without_folder"] == [rid]
    assert chk["runs_on_disk_without_row"] == ["20240101-000000-abcdef"]


def test_cli_preview_default(capsys):
    old = _mk_run(age_days=40)
    assert main(["data", "cleanup", "--days", "30"]) == 0
    assert json.loads(capsys.readouterr().out)["preview"] is True
    assert runs.run_dir(old).is_dir()
    assert main(["data", "cleanup", "--days", "30", "--execute"]) == 0
    assert not runs.run_dir(old).exists()
    assert main(["data", "status"]) == 0


def test_purge_unlinks_symlinks_never_follows(tmp_path):
    outside = tmp_path / "precious"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    _mk_run()
    root = home.ultron_home()
    _mark_home(root)
    try:
        (root / "runs" / "zzz-link").symlink_to(outside, target_is_directory=True)
        (root / "cv").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert maintenance.purge(yes=False)["preview"] is True
    assert (root / "ultron.sqlite3").exists()
    out = maintenance.purge(yes=True)
    assert out["preview"] is False
    assert (outside / "keep.txt").is_file()
    assert not (root / "ultron.sqlite3").exists() and not (root / "cv").exists()
    assert not list((root / "runs").iterdir())


def test_purge_validates_before_deleting(tmp_path, monkeypatch):
    _mk_run()
    root = home.ultron_home()
    _mark_home(root)
    real = maintenance._safe_target

    def boom(path, root_, **kw):
        if path.name == "cv":
            raise maintenance.MaintenanceError("unsafe_path", "x")
        return real(path, root_, **kw)

    (root / "cv").mkdir()
    monkeypatch.setattr(maintenance, "_safe_target", boom)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.purge(yes=True)
    assert (root / "ultron.sqlite3").exists() and (root / "cv").is_dir()


def test_delete_run_nonexistent_is_not_found():
    keep = _mk_run()
    with pytest.raises(maintenance.MaintenanceError) as err:
        maintenance.delete_run("20250101-000000-abcdef", yes=True)
    assert err.value.code == "not_found"
    assert runs.run_dir(keep).is_dir()


def test_run_id_trailing_newline_and_unicode_digits_rejected():
    for bad in ("20250101-000000-abcdef\n", "２0250101-000000-abcdef"):
        with pytest.raises(maintenance.MaintenanceError):
            maintenance.delete_run(bad, yes=True)


def test_purge_open_run_boundaries():
    root = home.ultron_home()
    young = _mk_run(finished=False, age_days=2 / 24)
    with pytest.raises(maintenance.MaintenanceError) as err:
        maintenance.purge(yes=True)
    assert err.value.code == "open_run"
    assert (root / "ultron.sqlite3").exists()
    maintenance.delete_run(young, yes=True)
    _mk_run(finished=False, age_days=25 / 24)
    assert maintenance.purge(yes=True)["preview"] is False


def test_backup_without_database_errors_and_creates_nothing():
    root = home.ultron_home()
    with pytest.raises(maintenance.MaintenanceError) as err:
        maintenance.backup()
    assert err.value.code == "not_found"
    assert not (root / "ultron.sqlite3").exists() and not (root / "backups").exists()


def test_runs_dir_resolving_outside_home_refused(tmp_path):
    outside = tmp_path / "outruns"
    outside.mkdir()
    rid = "20250101-000000-abcdef"
    (outside / rid).mkdir()
    root = home.ultron_home()
    (root / "runs").rmdir()
    try:
        (root / "runs").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.delete_run(rid, yes=True)
    with pytest.raises(maintenance.MaintenanceError):
        maintenance.cleanup(0, execute=True)
    assert (outside / rid).is_dir()


def test_remove_refuses_outside_and_home_itself(tmp_path):
    root = home.ultron_home()
    victim = tmp_path / "elsewhere"
    victim.mkdir()
    for bad in (victim, root, root / ".." / "elsewhere"):
        with pytest.raises(maintenance.MaintenanceError):
            maintenance._remove(bad, root)
    assert victim.is_dir() and root.is_dir()


def test_purge_spares_generic_folders_in_a_home_ultron_does_not_own(tmp_path, monkeypatch):
    shared = tmp_path / "shared"
    monkeypatch.setenv("ULTRON_HOME", str(shared))
    for name in ("cv", "backups"):
        (shared / name).mkdir(parents=True)
        (shared / name / "mine.txt").write_text("precious", encoding="utf-8")
    (shared / "thesis.docx").write_bytes(b"x")
    preview = maintenance.purge(yes=False, include_backups=True)
    assert not any(p.endswith(("cv", "backups")) for p in preview["would_delete"])
    maintenance.purge(yes=True, include_backups=True)
    assert (shared / "cv" / "mine.txt").is_file() and (shared / "backups" / "mine.txt").is_file()
    assert (shared / "thesis.docx").is_file()


def test_purge_in_owned_home_keeps_foreign_entries(tmp_path, monkeypatch):
    shared = tmp_path / "owned"
    monkeypatch.setenv("ULTRON_HOME", str(shared))
    (shared / "cv").mkdir(parents=True)
    (shared / "ultron.sqlite3").write_bytes(b"")
    _mark_home(shared)
    (shared / "thesis.docx").write_bytes(b"x")
    (shared / "notes").mkdir()
    maintenance.purge(yes=True, include_backups=True)
    assert not (shared / "cv").exists() and not (shared / "ultron.sqlite3").exists()
    assert (shared / "thesis.docx").is_file() and (shared / "notes").is_dir()


def test_cli_purge_spares_generic_folders_in_an_unowned_home(tmp_path, monkeypatch, capsys):
    shared = tmp_path / "shared-cli"
    monkeypatch.setenv("ULTRON_HOME", str(shared))
    for name in ("cv", "backups"):
        (shared / name).mkdir(parents=True)
        (shared / name / "mine.txt").write_text("precious", encoding="utf-8")
    assert main(["data", "purge", "--yes", "--include-backups"]) == 0
    capsys.readouterr()
    assert (shared / "cv" / "mine.txt").is_file() and (shared / "backups" / "mine.txt").is_file()
    assert not (shared / "ultron.sqlite3").exists()


def _shared_home_with_ledger(tmp_path, monkeypatch):
    shared = tmp_path / "shared-ledger"
    monkeypatch.setenv("ULTRON_HOME", str(shared))
    for rel in ("venv/x", "backups/x", "cv/mine/resume.pdf", "runs/x"):
        (shared / rel).parent.mkdir(parents=True, exist_ok=True)
        (shared / rel).write_bytes(b"precious")
    with closing(ledger.connect()):
        pass  # the CLI creates the ledger on first use
    assert (shared / "ultron.sqlite3").is_file()
    return shared


@pytest.mark.parametrize("extra", [[], ["--include-backups"]])
def test_cli_purge_with_only_ledger_keeps_preexisting_folders(tmp_path, monkeypatch, capsys, extra):
    shared = _shared_home_with_ledger(tmp_path, monkeypatch)
    assert main(["data", "purge", "--yes", *extra]) == 0
    capsys.readouterr()
    for rel in ("venv/x", "backups/x", "cv/mine/resume.pdf", "runs/x"):
        assert (shared / rel).read_bytes() == b"precious", rel
    assert not (shared / "ultron.sqlite3").exists()


def test_purge_with_only_ledger_still_removes_run_folders(tmp_path, monkeypatch):
    shared = _shared_home_with_ledger(tmp_path, monkeypatch)
    rid = _mk_run()
    preview = maintenance.purge(yes=False)
    assert not any(p.endswith(("cv", "backups")) for p in preview["would_delete"])
    assert str(shared / "runs" / "x") not in preview["would_delete"]
    maintenance.purge(yes=True)
    assert not (shared / "runs" / rid).exists()
    assert (shared / "runs" / "x").is_file() and (shared / "cv" / "mine" / "resume.pdf").is_file()


def test_restore_retains_a_note_acknowledged_during_safety_backup(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from ultron.storage import review

    _mk_run()
    source = maintenance.backup()
    copy = maintenance._copy_db
    writing, acknowledged = Event(), Event()

    def note():
        writing.set()
        review.add_note("k1", "synthetic committed edit")
        acknowledged.set()

    with ThreadPoolExecutor(max_workers=1) as executor:
        submissions = []

        def racing_copy(conn, target):
            copy(conn, target)
            if target.name.endswith("-pre-restore.sqlite3"):
                submissions.append(executor.submit(note))
                assert writing.wait(timeout=5)
                acknowledged.wait(timeout=0.5)

        monkeypatch.setattr(maintenance, "_copy_db", racing_copy)
        result = maintenance.restore(source, yes=True)
        for submission in submissions:
            submission.result(timeout=5)

    assert submissions and acknowledged.is_set()
    with closing(ledger.connect()) as live, closing(sqlite3.connect(result["previous_backup"])) as backup:
        observed = [conn.execute("SELECT notes FROM jobs WHERE identity_key = 'k1'").fetchone()[0]
                    for conn in (live, backup)]
    assert any("synthetic committed edit" in notes for notes in observed)
