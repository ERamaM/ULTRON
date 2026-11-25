"""``finalize`` for scout runs: export only what the agent judged, in the order the agent set.

A posting in the queue becomes a row only with an agent verdict of ``keep`` or ``unsure``. Postings left
unevaluated, and those beyond the queue cap, are recorded as discards and never exported. Seen postings whose
facts did not change are carried from memory with their earlier verdict. Every agent verdict is stored in the
scout memory, so a later run starts from it.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ultron.core.decision.decision import DecisionFinalizer
from ultron.core.decision.heuristics import HeuristicConfig
from ultron.core.final_row import assemble_final_row, fallback_fit_signals, prepare_geography
from ultron.core.models import DiscardRecord, ExtractedJob, FinalRow, JobEvaluation, RunSummary
from ultron.core.ranker import dedupe_final_rows
from ultron.core.ranker.final_rows import VERIFIED_FOUND_BY, rank_scout_rows
from ultron.core.remote_requirement import is_remote_required
from ultron.core.reporting.export import write_outputs
from ultron.core.rules.geography import geo_scope
from ultron.stages import scout_verified
from ultron.stages.final_helpers import TOP_N, candidate_of, discard_record, row_identity, seed_kept, stamp_first_seen
from ultron.stages.scout import CARRIED_FILE, NOT_EVALUATED_FILE, WATCH_PASS
from ultron.storage import ledger, runs, scout_memory, watchlist


class _Collector:
    """Rows, discards and memory verdicts gathered while finalizing a scout run."""

    def __init__(self) -> None:
        self.rows: list[FinalRow] = []
        self.identity: dict[int, str] = {}
        self.new: set[int] = set()
        self.discards: list[DiscardRecord] = []
        self.stage_discards: list[dict] = []
        self.verdicts: list[dict] = []
        self.unjudged_watch: list[str] = []

    def discard(self, record: DiscardRecord, key: str, stage: str) -> None:
        self.discards.append(record)
        self.stage_discards.append({**record.model_dump(mode="json"), "identity_key": key, "stage": stage})

    def add(self, row: FinalRow, key: str, *, new: bool) -> None:
        self.identity[id(row)] = key
        self.rows.append(row)
        if new:
            self.new.add(id(row))


def _judge(entry: dict, agent: JobEvaluation, out: _Collector, context: dict) -> None:
    """Turn one agent verdict into a row or a discard, and remember the verdict."""
    key, job = entry["identity_key"], ExtractedJob(**entry["job"])
    heuristic = JobEvaluation.model_validate(entry["heuristic"])
    evaluation = context["finalizer"].finalize(agent, from_llm=True, heuristic_baseline=heuristic)
    memory = {"identity_key": key, "verdict": evaluation.verdict if (evaluation.keep or evaluation.verdict == "unsure")
              else "discard", "discard_category": evaluation.discard_category or "", "priority": agent.priority,
              "content_hash": entry.get("content_hash")
              or scout_memory.content_hash(job.title, job.location, job.remote_text, job.body_text),
              "company": job.company, "row": None}
    out.verdicts.append(memory)
    if memory["verdict"] == "discard":
        out.discard(discard_record(job, evaluation.discard_reason or "llm_rejected"), key, "evaluator")
        return
    geography, geo_risk_note, geo_discard = prepare_geography(job, context["criteria"], geo=context["geo"])
    if geo_discard:
        memory["verdict"], memory["discard_category"] = "discard", "geo_conflict"
        out.discard(discard_record(job, geo_discard, "post_eval_geo_conflict"), key, "post_eval_geo")
        return
    row = assemble_final_row(
        job, evaluation, heur_remote_policy=geography.remote_policy, heur_eligibility=geography.eligibility,
        geo_red_flags=geography.red_flags, geo_risk_note=geo_risk_note,
        candidate=candidate_of(context["candidates"].get(key), job),
        fallback_fit_signals=context["fallback"], profile_criteria=context["criteria"], geo=context["geo"],
    )
    row.priority, row.found_by = agent.priority, str(entry.get("found_by") or "")
    memory["row"] = row
    out.add(row, key, new=entry.get("memory") == "new")


def _unjudged(out_dir: Path, queue: list[dict], agent: dict, out: _Collector, verified: dict) -> None:
    """Record queued postings the agent did not evaluate, and those beyond the queue cap (verified finds aside)."""
    beyond = [e for e in runs.read_jsonl(out_dir / NOT_EVALUATED_FILE) if e["identity_key"] not in verified]
    for entry, stage in [*((e, "not_evaluated") for e in queue if e["identity_key"] not in agent),
                         *((e, "queue_cap") for e in beyond)]:
        job = ExtractedJob(**entry["job"])
        out.discard(discard_record(job, "not_evaluated", stage), entry["identity_key"], stage)
        if entry.get("found_by") == WATCH_PASS:  # back to the watch's pending list for the next scout run
            out.unjudged_watch.append(entry["identity_key"])


def _carry(out_dir: Path, out: _Collector, verified: dict) -> int:
    carried = [item for item in runs.read_jsonl(out_dir / CARRIED_FILE) if item["identity_key"] not in verified]
    for item in carried:
        row = FinalRow.model_validate(item["row"])
        row.priority, row.found_by = item.get("priority"), "memory"
        out.add(row, item["identity_key"], new=False)
        if item.get("record_digest"):  # a verified find whose posting text was fetched for the first time
            out.verdicts.append({"identity_key": item["identity_key"], "verdict": row.evaluation_verdict,
                                 "priority": row.priority, "content_hash": item.get("content_hash") or "",
                                 "company": row.company, "row": row})
    return len(carried)


def _verified(verified: dict[str, dict], out: _Collector) -> int:
    """Add the agent's verified finds: ``keep``/``unsure`` rows lead the shortlist, every verdict is remembered."""
    exported = 0
    for found in verified.values():
        row = scout_verified.final_row(found) if found["verdict"] in scout_verified.EXPORTED_VERDICTS else None
        out.verdicts.append(scout_verified.memory_item(found, row))
        if row is not None:
            out.add(row, found["identity_key"], new=found.get("memory") == "new")
            exported += 1
    return exported


def finalize_scout(run_id: str, run: dict) -> dict:
    """Rank and export a scout run, record its history and the agent's verdicts."""
    out_dir = runs.run_dir(run_id)
    criteria = dict((run.get("profile_snapshot") or {}).get("criteria") or {})
    geo = geo_scope(run.get("profile_snapshot") or {})
    verified = scout_verified.load_verified(out_dir)
    # A posting the agent verified after the queue was built is settled by its verified judgment.
    queue = [e for e in runs.read_jsonl(out_dir / "ambiguous.jsonl") if e["identity_key"] not in verified]
    current = {e["identity_key"] for e in queue}
    agent = {r["identity_key"]: JobEvaluation.model_validate(r["evaluation"])
             for r in runs.read_jsonl(out_dir / "evaluations.jsonl") if r["identity_key"] in current}
    context = {
        "finalizer": DecisionFinalizer(HeuristicConfig(recall_mode_active=False, geo=geo), is_llama_cpp=lambda: False),
        "criteria": criteria, "geo": geo, "fallback": fallback_fit_signals(criteria),
        "candidates": {r["identity_key"]: r.get("candidate") for r in runs.read_jsonl(out_dir / "postings.jsonl")},
    }
    out = _Collector()
    out.discards = [DiscardRecord(**{k: v for k, v in d.items() if k in DiscardRecord.model_fields})
                    for d in runs.read_jsonl(out_dir / "discarded.jsonl")]
    for entry in queue:
        if entry["identity_key"] in agent:
            _judge(entry, agent[entry["identity_key"]], out, context)
    _unjudged(out_dir, queue, agent, out, verified)
    carried = _carry(out_dir, out, verified)
    verified_count = _verified(verified, out)

    deduped, duplicates = dedupe_final_rows(out.rows, merge_locations=is_remote_required(criteria))
    for row in duplicates:
        out.discard(DiscardRecord(apply_url=row.apply_url, reason="duplicate_role",
                                  detail=f"duplicate of earlier role company={row.company} title={row.title}",
                                  title=row.title, company=row.company, location=row.location),
                    out.identity[id(row)], "dedup")
    ranked = rank_scout_rows(deduped, out.new)
    max_roles = max(0, int(run["settings"].get("max_roles") or 0))
    capped = ranked[max_roles:] if max_roles else []
    ranked = ranked[:max_roles] if max_roles else ranked
    for row in capped:
        out.discard(DiscardRecord(apply_url=row.apply_url, reason="final_cap", detail=f"ranked below max_roles={max_roles}",
                                  title=row.title, company=row.company, location=row.location),
                    out.identity[id(row)], "final_cap")
    return _write(run_id, run, out_dir, ranked, out, {"agent_evaluated": len(agent), "carried_from_memory": carried,
                                                      "final_cap": len(capped), "candidates": context["candidates"],
                                                      "verified_finds": verified_count})


def _write(run_id: str, run: dict, out_dir: Path, ranked: list[FinalRow], out: _Collector, stats: dict) -> dict:
    started_at = datetime.fromisoformat(run["created_at"])
    if "finalized_at" not in run:
        run["finalized_at"] = datetime.now(started_at.tzinfo).isoformat()
        runs._write_run(run_id, run)
    counts: dict[str, int] = {}
    for record in out.discards:
        counts[record.reason] = counts.get(record.reason, 0) + 1
    summary = RunSummary(
        run_id=run_id, started_at=started_at, ended_at=datetime.fromisoformat(run["finalized_at"]),
        ats_pages_opened=len(stats["candidates"]), roles_kept=len(ranked), discard_counts=counts,
        full_query_log=[], unsure_to_keep_count=sum(1 for r in ranked if r.evaluation_verdict == "unsure"),
    )
    with closing(ledger.connect()) as conn:
        is_new = ledger.record_finalized_run(conn, summary, ranked, profile_slug=run["profile_slug"],
                                             preset=run["preset"], seed_kept=seed_kept(out_dir, ranked))
        scout_memory.record_verdicts(conn, run["profile_slug"], run_id, out.verdicts,
                                     scout_memory.profile_hash(run.get("profile_snapshot") or {}))
        watchlist.return_to_pending(conn, run["profile_slug"], out.unjudged_watch)
        stamp_first_seen(conn, run_id, ranked)
    md, js, sm = write_outputs(str(out_dir.parent), run_id, ranked, summary, out.discards)
    payload = json.loads(Path(js).read_text(encoding="utf-8"))
    for row, data in zip(ranked, payload["rows"], strict=True):
        data["is_new"] = is_new[row_identity(row)]
    Path(js).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    finalize_discards = out_dir / "finalize_discards.jsonl"
    runs.write_jsonl(finalize_discards, out.stage_discards)
    runs.mark_stage(run_id, "finalize")
    not_evaluated = sum(1 for d in out.stage_discards if d["reason"] == "not_evaluated")
    return {
        "mode": "scout",
        "roles": len(ranked),
        "new_roles": sum(1 for r in ranked if is_new[row_identity(r)]),
        "unsure": sum(1 for r in ranked if r.evaluation_verdict == "unsure"),
        "discarded": len(out.discards),
        "final_cap": stats["final_cap"],
        "agent_evaluated": stats["agent_evaluated"],
        "not_evaluated": not_evaluated,
        "carried_from_memory": stats["carried_from_memory"],
        "verified_finds": stats["verified_finds"],
        "extras": sum(1 for r in ranked if r.found_by != VERIFIED_FOUND_BY),
        "outputs": {"results_md": md, "results_json": js, "run_summary_md": sm,
                    "finalize_discards_jsonl": str(finalize_discards)},
        "top": [{"title": r.title, "company": r.company, "priority": r.priority, "is_new": is_new[row_identity(r)]}
                for r in ranked[:TOP_N]],
    }
