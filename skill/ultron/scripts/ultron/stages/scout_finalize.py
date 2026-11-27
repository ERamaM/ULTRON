"""Finalize a saved shortlist: the agent's judged postings ranked, exported and remembered.

``ultron shortlist save`` records the agent's rows as verified finds (``scout_verified``) in a run of mode
``scout`` and calls ``finalize_scout``. Every ``keep`` and ``unsure`` becomes a row in the agent's order (verdict,
then priority, then eligibility); every verdict, discards included, is stored in the scout memory so later
searches flag it.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ultron.core.models import DiscardRecord, FinalRow, RunSummary
from ultron.core.ranker import dedupe_final_rows
from ultron.core.ranker.final_rows import rank_scout_rows
from ultron.core.remote_requirement import is_remote_required
from ultron.core.reporting.export import write_outputs
from ultron.stages import scout_verified
from ultron.stages.final_helpers import TOP_N, row_identity, seed_kept
from ultron.storage import run_publication, runs


class _Collector:
    """Rows, discards and memory verdicts gathered while finalizing a saved shortlist."""

    def __init__(self) -> None:
        self.rows: list[FinalRow] = []
        self.identity: dict[int, str] = {}
        self.new: set[int] = set()
        self.discards: list[DiscardRecord] = []
        self.stage_discards: list[dict] = []
        self.verdicts: list[dict] = []

    def discard(self, row: FinalRow, reason: str, detail: str, stage: str) -> None:
        record = DiscardRecord(apply_url=row.apply_url, reason=reason, detail=detail, title=row.title,
                               company=row.company, location=row.location)
        self.discards.append(record)
        self.stage_discards.append({**record.model_dump(mode="json"), "identity_key": self.identity[id(row)],
                                    "stage": stage})

    def add(self, row: FinalRow, key: str, *, new: bool) -> None:
        self.identity[id(row)] = key
        self.rows.append(row)
        if new:
            self.new.add(id(row))


def _verified(verified: dict[str, dict], out: _Collector) -> int:
    """Add the agent's verified finds: ``keep``/``unsure`` rows are exported, every verdict is remembered."""
    exported = 0
    for found in verified.values():
        row = scout_verified.final_row(found) if found["verdict"] in scout_verified.EXPORTED_VERDICTS else None
        out.verdicts.append(scout_verified.memory_item(found, row))
        if row is not None:
            out.add(row, found["identity_key"], new=found.get("memory") == "new")
            exported += 1
    return exported


def finalize_scout(run_id: str, run: dict) -> dict:
    """Rank and export a saved shortlist, record its history and the agent's verdicts."""
    out_dir = runs.run_dir(run_id)
    criteria = dict((run.get("profile_snapshot") or {}).get("criteria") or {})
    out = _Collector()
    verified_count = _verified(scout_verified.load_verified(out_dir), out)
    deduped, duplicates = dedupe_final_rows(out.rows, merge_locations=is_remote_required(criteria))
    for row in duplicates:
        out.discard(row, "duplicate_role", f"duplicate of earlier role company={row.company} title={row.title}",
                    "dedup")
    ranked = rank_scout_rows(deduped)
    max_roles = max(0, int(run["settings"].get("max_roles") or 0))
    capped = ranked[max_roles:] if max_roles else []
    ranked = ranked[:max_roles] if max_roles else ranked
    for row in capped:
        out.discard(row, "final_cap", f"ranked below max_roles={max_roles}", "final_cap")
    return _write(run_id, run, out_dir, ranked, out, {"final_cap": len(capped), "verified_finds": verified_count})


def _write(run_id: str, run: dict, out_dir: Path, ranked: list[FinalRow], out: _Collector, stats: dict) -> dict:
    started_at = datetime.fromisoformat(run["created_at"])
    if "finalized_at" not in run:
        run["finalized_at"] = datetime.now(started_at.tzinfo).isoformat()
    counts: dict[str, int] = {}
    for record in out.discards:
        counts[record.reason] = counts.get(record.reason, 0) + 1
    summary = RunSummary(
        run_id=run_id, started_at=started_at, ended_at=datetime.fromisoformat(run["finalized_at"]),
        ats_pages_opened=0, roles_kept=len(ranked), discard_counts=counts,
        full_query_log=[], unsure_to_keep_count=sum(1 for r in ranked if r.evaluation_verdict == "unsure"),
    )
    is_new, outputs = run_publication.publish_run(
        run, summary, ranked, out.discards, out.stage_discards,
        seeds=seed_kept(out_dir, ranked), writer=write_outputs, scout_verdicts=out.verdicts,
    )
    return {
        "roles": len(ranked),
        "new_roles": sum(1 for r in ranked if is_new[row_identity(r)]),
        "unsure": sum(1 for r in ranked if r.evaluation_verdict == "unsure"),
        "discarded": len(out.discards),
        "final_cap": stats["final_cap"],
        "verified_finds": stats["verified_finds"],
        "outputs": outputs,
        "top": [{"title": r.title, "company": r.company, "priority": r.priority, "is_new": is_new[row_identity(r)]}
                for r in ranked[:TOP_N]],
    }
