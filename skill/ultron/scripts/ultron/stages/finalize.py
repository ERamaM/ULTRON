"""``finalize``: merge verdicts, build final rows, rank, cap, record history and export."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime
from pathlib import Path

from ultron.core.decision.decision import DecisionFinalizer
from ultron.core.decision.heuristics import HeuristicConfig
from ultron.core.final_row import _assemble_final_row, fallback_fit_signals, prepare_geography
from ultron.core.models import DiscardRecord, ExtractedJob, FinalRow, JobEvaluation, RunSummary
from ultron.core.ranker import dedupe_final_rows, rank_final_rows
from ultron.core.remote_requirement import is_remote_required
from ultron.core.reporting.export import write_outputs
from ultron.core.rules.geography import geo_scope, other_country_restriction
from ultron.core.rules.hard_filters import is_full_remote_only
from ultron.stages.final_helpers import TOP_N, candidate_of, discard_record, row_identity, seed_kept
from ultron.stages.scout_finalize import finalize_scout
from ultron.storage import ledger, runs


def finalize(run_id: str) -> dict:
    """Rank, deduplicate, export reports, and update ledger history for ``run_id`` (scout runs too)."""
    runs.require_stage(run_id, "finalize")
    run = runs.load_run(run_id)
    if runs.run_mode(run) == "scout":
        return finalize_scout(run_id, run)
    out_dir = runs.run_dir(run_id)
    criteria = dict((run.get("profile_snapshot") or {}).get("criteria") or {})
    geo = geo_scope(run.get("profile_snapshot") or {})
    max_roles = max(0, int(run["settings"].get("max_roles") or 0))

    entries = [*runs.read_jsonl(out_dir / "kept.jsonl"), *runs.read_jsonl(out_dir / "ambiguous.jsonl")]
    current = {e["identity_key"] for e in entries}
    # Evaluations for identity keys outside the current filter output are stale; ignore them.
    agent = {
        r["identity_key"]: JobEvaluation.model_validate(r["evaluation"])
        for r in runs.read_jsonl(out_dir / "evaluations.jsonl")
        if r["identity_key"] in current
    }
    candidates = {r["identity_key"]: r.get("candidate") for r in runs.read_jsonl(out_dir / "postings.jsonl")}

    finalizer = DecisionFinalizer(HeuristicConfig(recall_mode_active=False, geo=geo), is_llama_cpp=lambda: False)
    fallback = fallback_fit_signals(criteria)

    raw_rows: list[FinalRow] = []
    row_keys: dict[int, str] = {}
    stage_discards: list[dict] = []

    def record(rec: DiscardRecord, key: str, stage: str) -> None:
        discards.append(rec)
        stage_discards.append({**rec.model_dump(mode="json"), "identity_key": key, "stage": stage})

    discards: list[DiscardRecord] = [
        DiscardRecord(**{k: v for k, v in d.items() if k in DiscardRecord.model_fields})
        for d in runs.read_jsonl(out_dir / "discarded.jsonl")
    ]
    for entry in entries:
        key = entry["identity_key"]
        job = ExtractedJob(**entry["job"])
        heuristic = JobEvaluation.model_validate(entry["heuristic"])
        if key in agent:
            evaluation = finalizer.finalize(agent[key], from_llm=True, heuristic_baseline=heuristic)
        else:
            evaluation = finalizer.finalize(heuristic, from_llm=False)
        if not (evaluation.keep or evaluation.verdict == "unsure"):
            record(discard_record(job, evaluation.discard_reason or "llm_rejected"), key, "evaluator")
            continue
        geography, geo_risk_note, geo_discard = prepare_geography(job, criteria, geo=geo)
        if geo_discard:
            record(discard_record(job, geo_discard, "post_eval_geo_conflict"), key, "post_eval_geo")
            continue
        # Heuristic keeps limited to a country outside the profile's home places need a check: "Remote across
        # Germany" is not open to someone in Spain. Agent verdicts already answered the eligibility question.
        limited_to = None if key in agent or evaluation.verdict != "keep" else other_country_restriction(
            job.location, job.remote_text, criteria, job.body_text,
            remote_job=is_full_remote_only(job.remote_text, job.body_text),
        )
        if limited_to:
            evaluation = evaluation.model_copy(update={"verdict": "unsure", "keep": False, "eligibility": "unknown"})
            geo_risk_note = f"location limited to {limited_to}"
        row = _assemble_final_row(
            job, evaluation,
            heur_remote_policy=geography.remote_policy,
            heur_eligibility=geography.eligibility,
            geo_red_flags=geography.red_flags,
            geo_risk_note=geo_risk_note,
            candidate=candidate_of(candidates.get(key), job),
            fallback_fit_signals=fallback,
            profile_criteria=criteria,
            geo=geo,
        )
        if limited_to:
            row.eligibility = "unknown"
        row_keys[id(row)] = key
        raw_rows.append(row)

    # A remote-only profile can work any listed city, so one posting listed once per city is one row.
    deduped, duplicates = dedupe_final_rows(raw_rows, merge_locations=is_remote_required(criteria))
    for row in duplicates:
        record(DiscardRecord(
            apply_url=row.apply_url, reason="duplicate_role",
            detail=f"duplicate of earlier role company={row.company} title={row.title}",
            title=row.title, company=row.company, location=row.location,
        ), row_keys[id(row)], "dedup")
    ranked = rank_final_rows(deduped, use_match_score=True)
    capped: list[FinalRow] = []
    if max_roles and len(ranked) > max_roles:
        capped = ranked[max_roles:]
        ranked = ranked[:max_roles]
    for row in capped:
        record(DiscardRecord(
            apply_url=row.apply_url, reason="final_cap", detail=f"ranked below max_roles={max_roles}",
            title=row.title, company=row.company, location=row.location,
        ), row_keys[id(row)], "final_cap")

    # A fixed end timestamp keeps repeated finalizes byte-identical.
    started_at = datetime.fromisoformat(run["created_at"])
    if "finalized_at" not in run:
        run["finalized_at"] = datetime.now(started_at.tzinfo).isoformat()
        runs._write_run(run_id, run)
    ended_at = datetime.fromisoformat(run["finalized_at"])
    counts: dict[str, int] = {}
    for d in discards:
        counts[d.reason] = counts.get(d.reason, 0) + 1
    query_hits: dict[str, dict] = {}
    for cand in candidates.values():
        if not isinstance(cand, dict):
            continue
        q = str(cand.get("query") or "").strip()
        if not q:
            continue
        entry_q = query_hits.get(q)
        if entry_q is None:
            query_hits[q] = {"query_id": str(cand.get("query_id") or ""), "query": q, "hits": 1}
        else:
            entry_q["hits"] += 1
    summary = RunSummary(
        run_id=run_id, started_at=started_at, ended_at=ended_at,
        ats_pages_opened=len(candidates), roles_kept=len(ranked), discard_counts=counts,
        full_query_log=list(query_hits.values()),
        unsure_to_keep_count=sum(1 for r in ranked if r.evaluation_verdict == "unsure"),
    )

    with closing(ledger.connect()) as conn:
        is_new = ledger.record_finalized_run(
            conn, summary, ranked, profile_slug=run["profile_slug"], preset=run["preset"],
            seed_kept=seed_kept(out_dir, ranked),
        )

        first_seen = ledger.first_observations(conn, run_id)
    for row in ranked:
        row.first_seen = first_seen.get(row_identity(row))
    md, js, sm = write_outputs(str(out_dir.parent), run_id, ranked, summary, discards)
    # write_outputs (ported verbatim) has no is_new notion; patch it into each results.json row.
    payload = json.loads(Path(js).read_text(encoding="utf-8"))
    for row, data in zip(ranked, payload["rows"], strict=True):
        data["is_new"] = is_new[row_identity(row)]
    Path(js).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    finalize_discards = out_dir / "finalize_discards.jsonl"
    runs.write_jsonl(finalize_discards, stage_discards)  # atomic rewrite: idempotent, discarded.jsonl untouched

    runs.mark_stage(run_id, "finalize")
    return {
        "roles": len(ranked),
        "new_roles": sum(1 for r in ranked if is_new[row_identity(r)]),
        "unsure": sum(1 for r in ranked if r.evaluation_verdict == "unsure"),
        "discarded": len(discards),
        "final_cap": len(capped),
        "agent_evaluated": len(agent),
        "heuristic_fallback": len(current - agent.keys()),
        "outputs": {"results_md": md, "results_json": js, "run_summary_md": sm,
                    "finalize_discards_jsonl": str(finalize_discards)},
        "top": [
            {"title": r.title, "company": r.company, "match_score": r.match_score, "is_new": is_new[row_identity(r)]}
            for r in ranked[:TOP_N]
        ],
    }
