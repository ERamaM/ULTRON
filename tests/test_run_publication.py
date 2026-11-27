"""Verify rollback when publishing a staged run report fails."""

from contextlib import closing

import pytest

from ultron.profiles import store as profiles
from ultron.stages.scout_finalize import finalize_scout
from ultron.storage import ledger, run_publication, runs
from ultron.toolkit.shortlist import save_shortlist


def test_partial_publication_restores_all_previous_files(tmp_path, monkeypatch):
    profiles.install_example()
    result = save_shortlist(profiles.EXAMPLE_SLUG, [
        {"url": "https://example.com/jobs/5", "title": "ML Engineer", "company": "Demo", "verdict": "keep"},
    ])
    run_id = result["run_id"]
    directory = runs.run_dir(run_id)
    originals = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    replace = run_publication.os.replace

    def fail(source, target):
        if target == directory / "run_summary.md" and source.parent.name == run_id:
            raise OSError("synthetic publication failure")
        replace(source, target)

    monkeypatch.setattr(run_publication.os, "replace", fail)
    with pytest.raises(OSError, match="synthetic publication failure"):
        finalize_scout(run_id, runs.load_run(run_id))
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == originals
    assert not list(directory.glob(".publication-*"))
    with closing(ledger.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM job_occurrences WHERE run_id = ?", (run_id,)).fetchone()[0] == 1
