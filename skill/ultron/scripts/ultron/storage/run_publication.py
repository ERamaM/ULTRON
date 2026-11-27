"""Coordinate ledger transactions and report publication for completed runs."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import DiscardRecord, FinalRow, RunSummary
from ultron.infra.fs import atomic_write_text
from ultron.storage import ledger, runs, scout_memory

_FILES = {
    "results_md": "results.md", "results_json": "results.json", "run_summary_md": "run_summary.md",
    "finalize_discards_jsonl": "finalize_discards.jsonl",
}


@contextmanager
def _publish_files(staged: Path, dest: Path) -> Iterator[None]:
    previous = staged.parent / "previous"
    previous.mkdir()
    names = (*_FILES.values(), "run.json")
    for name in names:
        if (dest / name).is_file():
            shutil.copyfile(dest / name, previous / name)
    replaced: list[str] = []
    try:
        for name in names:
            os.replace(staged / name, dest / name)
            replaced.append(name)
        yield
    except BaseException:
        for name in reversed(replaced):
            saved = previous / name
            if saved.exists():
                os.replace(saved, dest / name)
            else:
                (dest / name).unlink(missing_ok=True)
        raise


def publish_run(
    run: dict, summary: RunSummary, rows: list[FinalRow], discards: list[DiscardRecord],
    stage_discards: list[dict], *, seeds: dict[str, int] | None, writer: Callable,
    scout_verdicts: list[dict] | None = None,
) -> tuple[dict[str, bool], dict[str, str]]:
    """Publish a run's complete report and ledger history, restoring prior outputs on failure."""
    run_id = summary.run_id
    directory = runs.run_dir(run_id)
    profile = run["profile_slug"]
    search_id = run.get("saved_search_id")
    with (
        TemporaryDirectory(prefix=".publication-", dir=directory) as temporary,
        ExitStack() as published,
        closing(ledger.connect()) as conn,
        ledger.finalized_run_transaction(
            conn, summary, rows, profile_slug=profile, preset=run["preset"], seed_kept=seeds, saved_search_id=search_id,
        ) as is_new,
    ):
        first_seen = ledger.first_observations(conn, run_id)
        new_to_search = ledger.new_keys_in_saved_search(conn, run_id) if search_id else None
        annotations = []
        for row in rows:
            key = canonical_job_identity(row.apply_url, source_type=row.source_type, source_key=row.source_key).identity_key
            row.first_seen = first_seen.get(key)
            flags = {"is_new": is_new[key]}
            if new_to_search is not None:
                flags["is_new_in_saved_search"] = key in new_to_search
            annotations.append(flags)
        if scout_verdicts is not None:
            scout_memory.record_verdicts(conn, profile, run_id, scout_verdicts,
                                         scout_memory.profile_hash(run.get("profile_snapshot") or {}), commit=False)
        writer(temporary, run_id, rows, summary, discards)
        staged = Path(temporary) / run_id
        results = staged / _FILES["results_json"]
        payload = json.loads(results.read_text(encoding="utf-8"))
        for row, flags in zip(payload["rows"], annotations, strict=True):
            row.update(flags)
        atomic_write_text(results, json.dumps(payload, indent=2, ensure_ascii=False))
        runs.write_jsonl(staged / _FILES["finalize_discards_jsonl"], stage_discards)
        finished = {**run, "stages_done": list(dict.fromkeys([*run["stages_done"], "finalize"]))}
        atomic_write_text(staged / "run.json", json.dumps(finished, ensure_ascii=False, indent=2))
        published.enter_context(_publish_files(staged, directory))
    return is_new, {key: str(directory / name) for key, name in _FILES.items()}
