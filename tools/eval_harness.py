"""Offline replay core shared by the quality tools.

It runs the real ``filter`` and ``finalize`` stages over a list of postings inside a throwaway
``ULTRON_HOME``. Nothing is fetched, no agent is called, and the caller's own ``ULTRON_HOME``
(ledger, seeds, runs) is never opened or written: finalize's ledger writes and exports land in
the temporary folder, which is deleted afterwards.

The result maps every posting identity to the outcome of each stage, so callers can compute
metrics (``quality_benchmark.py``) or diff two replays (``replay_run.py``).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ultron.stages import finalize as finalize_stage
from ultron.stages.filter import filter_run
from ultron.storage import runs

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


def assign_ranks(
    entries: list[dict[str, Any]], discarded: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Final outcome of every identity that reached finalize.

    ``entries`` are the kept and ambiguous filter rows, ``discarded`` maps identities to their
    ``finalize_discards.jsonl`` row and ``rows`` are the shortlist rows in rank order. An identity is
    ranked exactly when finalize did not discard it; the shortlist rows (joined by ``apply_url``,
    first unassigned identity first) only add its rank, verdict and score.
    """
    outcomes: dict[str, dict[str, Any]] = {}
    pending: dict[str, list[str]] = {}
    for entry in entries:
        key = entry["identity_key"]
        if key in discarded:
            row = discarded[key]
            outcomes[key] = {"status": "discarded", "stage": row.get("stage"), "reason": row.get("reason")}
        elif key not in outcomes:
            outcomes[key] = {"status": "ranked", "rank": None, "verdict": None}
            pending.setdefault(str((entry.get("job") or {}).get("apply_url") or ""), []).append(key)
    for rank, row in enumerate(rows, start=1):
        keys = pending.get(str(row.get("apply_url") or ""))
        if not keys:
            continue
        outcomes[keys.pop(0)] = {
            "status": "ranked",
            "rank": rank,
            "verdict": row.get("evaluation_verdict"),
            "match_score": row.get("match_score"),
            "match_band": row.get("match_band"),
        }
    return outcomes


def read_final_outcomes(out_dir: Path, rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Load kept, ambiguous, and discarded outputs for one run with assigned ranks."""
    entries = [*runs.read_jsonl(out_dir / "kept.jsonl"), *runs.read_jsonl(out_dir / "ambiguous.jsonl")]
    discarded = {row["identity_key"]: row for row in runs.read_jsonl(out_dir / "finalize_discards.jsonl")}
    return assign_ranks(entries, discarded, rows)


@contextmanager
def _capture_shortlist(store: list[dict[str, Any]]) -> Iterator[None]:
    """Keep finalize's ranked rows before export redaction (redaction can rewrite URLs)."""
    original = finalize_stage.write_outputs

    def capture(output_dir, run_id, rows, summary, discards):
        store[:] = [row.model_dump(mode="json") for row in rows]
        return original(output_dir, run_id, rows, summary, discards)

    finalize_stage.write_outputs = capture
    try:
        yield
    finally:
        finalize_stage.write_outputs = original


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
        shortlist: list[dict[str, Any]] = []
        with _capture_shortlist(shortlist):
            finalize_summary = finalize_stage.finalize(run_id)
        filtered = _filter_outcomes(out_dir)
        final = read_final_outcomes(out_dir, shortlist)
    ranked = [o for o in final.values() if o["status"] == "ranked"]
    if len(shortlist) != finalize_summary["roles"] or any(o["rank"] is None for o in ranked):
        raise RuntimeError("replay could not map every shortlist row to a posting identity")
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
