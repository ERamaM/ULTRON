"""Offline replay core shared by the quality tools.

It runs the real ``filter`` and ``finalize`` stages over a list of postings inside a throwaway
``ULTRON_HOME``. Nothing is fetched, no agent is called, and the caller's own ``ULTRON_HOME``
(ledger, seeds, runs) is never opened or written: finalize's ledger writes and exports land in
the temporary folder, which is deleted afterwards.

The result maps every posting identity to the outcome of each stage, so callers can compute
metrics (``quality_benchmark.py``) or diff two replays (``replay_run.py``).
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ultron import runs
from ultron.stages.filter import filter_run
from ultron.stages.finalize import finalize

REPLAY_RUN_ID = "20000101-000000-000000"
REPLAY_CREATED_AT = "2000-01-01T00:00:00+00:00"


@contextmanager
def isolated_home() -> Iterator[Path]:
    """Point ``ULTRON_HOME`` at a fresh temporary folder, restoring the previous value afterwards."""
    previous = os.environ.get("ULTRON_HOME")
    with tempfile.TemporaryDirectory(prefix="ultron-replay-") as tmp:
        os.environ["ULTRON_HOME"] = tmp
        try:
            yield Path(tmp)
        finally:
            if previous is None:
                os.environ.pop("ULTRON_HOME", None)
            else:
                os.environ["ULTRON_HOME"] = previous


def _filter_outcomes(out_dir: Path) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    for row in runs.read_jsonl(out_dir / "discarded.jsonl"):
        outcomes[row["identity_key"]] = {"route": "discarded", "stage": row.get("stage"), "reason": row.get("reason")}
    for name, route in (("kept.jsonl", "kept"), ("ambiguous.jsonl", "ambiguous")):
        for row in runs.read_jsonl(out_dir / name):
            heuristic = row.get("heuristic") or {}
            outcomes[row["identity_key"]] = {
                "route": route,
                "verdict": heuristic.get("verdict"),
                "ambiguity": row.get("ambiguity"),
            }
    return outcomes


def _final_outcomes(out_dir: Path) -> dict[str, dict[str, Any]]:
    """Final rank (1-based) or finalize-stage discard for every identity that reached finalize."""
    entries = [*runs.read_jsonl(out_dir / "kept.jsonl"), *runs.read_jsonl(out_dir / "ambiguous.jsonl")]
    by_url: dict[str, str] = {}
    for entry in entries:
        by_url.setdefault(str((entry.get("job") or {}).get("apply_url") or ""), entry["identity_key"])
    outcomes: dict[str, dict[str, Any]] = {}
    for row in runs.read_jsonl(out_dir / "finalize_discards.jsonl"):
        outcomes[row["identity_key"]] = {"status": "discarded", "stage": row.get("stage"), "reason": row.get("reason")}
    results = json.loads((out_dir / "results.json").read_text(encoding="utf-8"))
    for rank, row in enumerate(results.get("rows") or [], start=1):
        key = by_url.get(str(row.get("apply_url") or ""))
        if key is None:
            continue
        outcomes[key] = {
            "status": "ranked",
            "rank": rank,
            "verdict": row.get("evaluation_verdict"),
            "match_score": row.get("match_score"),
            "match_band": row.get("match_band"),
        }
    return outcomes


def replay(
    postings: list[dict[str, Any]],
    *,
    profile: dict[str, Any],
    settings: dict[str, Any],
    evaluations: list[dict[str, Any]] | None = None,
    profile_slug: str = "replay",
    preset: str = "replay",
) -> dict[str, Any]:
    """Run ``filter`` then ``finalize`` over ``postings`` and return per-identity outcomes.

    ``postings`` are ``postings.jsonl`` rows (``identity_key``, ``job`` and optionally ``candidate``
    and ``ats_fingerprint``). ``evaluations`` are ``evaluations.jsonl`` rows; finalize uses them for
    identities that are still in the filter output and falls back to the heuristic otherwise.
    """
    with isolated_home():
        run_id = REPLAY_RUN_ID
        out_dir = runs.run_dir(run_id)
        out_dir.mkdir(parents=True)
        runs._write_run(run_id, {
            "run_id": run_id,
            "profile_slug": profile_slug,
            "preset": preset,
            "settings": dict(settings),
            "profile_snapshot": profile,
            "stages_done": ["fetch"],
            "created_at": REPLAY_CREATED_AT,
            "finalized_at": REPLAY_CREATED_AT,
        })
        runs.write_jsonl(out_dir / "postings.jsonl", postings)
        if evaluations:
            runs.write_jsonl(out_dir / "evaluations.jsonl", evaluations)
        filter_summary = filter_run(run_id)
        finalize_summary = finalize(run_id)
        filtered = _filter_outcomes(out_dir)
        final = _final_outcomes(out_dir)
    identities = [str(row["identity_key"]) for row in postings]
    return {
        "filter_summary": filter_summary,
        "finalize_summary": {key: finalize_summary[key] for key in ("roles", "unsure", "discarded", "final_cap")},
        "identities": {
            key: {"filter": filtered.get(key), "final": final.get(key)} for key in dict.fromkeys(identities)
        },
    }


def final_status(outcome: dict[str, Any]) -> str:
    """Collapse one identity outcome to ``keep``, ``unsure`` or ``discard`` (what the shortlist shows)."""
    final = outcome.get("final") or {}
    if final.get("status") == "ranked":
        return "unsure" if final.get("verdict") == "unsure" else "keep"
    return "discard"


def losing_stage(outcome: dict[str, Any]) -> str | None:
    """First stage that kept an identity out of the shortlist as a confident keep (``None`` when it is one)."""
    filtered = outcome.get("filter") or {}
    if filtered.get("route") == "discarded":
        return str(filtered.get("stage") or "filter")
    final = outcome.get("final") or {}
    if final.get("status") == "discarded":
        return str(final.get("stage") or "finalize")
    if final.get("status") == "ranked" and final.get("verdict") == "unsure":
        return "unsure_in_shortlist"
    if final.get("status") != "ranked":
        return "missing"
    return None
