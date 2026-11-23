"""``finalize``: merge verdicts, build final rows, rank, cap, record history and export."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from ultron.core.ats.identity import seed_identity
from ultron.core.decision.decision import DecisionFinalizer
from ultron.core.decision.heuristics import HeuristicConfig
from ultron.core.final_row import _assemble_final_row, fallback_fit_signals, prepare_geography
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import CandidateUrl, DiscardRecord, ExtractedJob, FinalRow, JobEvaluation, RunSummary
from ultron.core.ranker import dedupe_final_rows, rank_final_rows
from ultron.core.remote_requirement import is_remote_required
from ultron.core.reporting.export import write_outputs
from ultron.core.rules.geography import geo_scope, other_country_restriction
from ultron.core.rules.hard_filters import is_full_remote_only
from ultron.stages.discover import SEED_ATTEMPTS_FILE
from ultron.storage import ledger, runs

TOP_N = 5


def _candidate(raw: dict | None, job: ExtractedJob) -> CandidateUrl:
    try:
        return CandidateUrl.model_validate(raw or {})
    except ValidationError:
        return CandidateUrl(
            query_id="", query="", source_url=job.apply_url, normalized_url=job.apply_url, domain=job.domain or ""
        )


def _discard(job: ExtractedJob, reason: str, detail: str = "") -> DiscardRecord:
    return DiscardRecord(
        apply_url=job.apply_url, reason=reason, detail=detail, title=job.title, company=job.company,
        location=job.location, remote_text=job.remote_text,
    )


def _row_key(row: FinalRow) -> str:
    return canonical_job_identity(row.apply_url, source_type=row.source_type, source_key=row.source_key).identity_key


def _seed_yield(out_dir: Path, rows: list[FinalRow]) -> tuple[dict[str, int] | None, dict[str, str]]:
    """Kept postings per seed this run queried (``None`` when ``discover ats`` recorded no attempts)."""
    path = out_dir / SEED_ATTEMPTS_FILE
    if not path.exists():
        return None, {}
    outcomes = {
        row["seed_key"]: row.get("status") or "unknown"
        for row in runs.read_jsonl(path) if isinstance(row, dict) and row.get("seed_key")
    }
    kept = dict.fromkeys(outcomes, 0)
    for row in rows:
        vendor, sep, board = str(row.source_key or "").partition(":")
        if row.source_type != "ats_api" or not sep:
            continue
        key = ":".join(seed_identity(vendor, board))
        if key in kept:
            kept[key] += 1
    return kept, outcomes


def finalize(run_id: str) -> dict:
    """Rank, deduplicate, export reports, and update ledger history for ``run_id``."""
    runs.require_stage(run_id, "finalize")
    run = runs.load_run(run_id)
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
    row_identity: dict[int, str] = {}
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
            record(_discard(job, evaluation.discard_reason or "llm_rejected"), key, "evaluator")
            continue
        geography, geo_risk_note, geo_discard = prepare_geography(job, criteria, geo=geo)
        if geo_discard:
            record(_discard(job, geo_discard, "post_eval_geo_conflict"), key, "post_eval_geo")
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
            candidate=_candidate(candidates.get(key), job),
            fallback_fit_signals=fallback,
            profile_criteria=criteria,
            geo=geo,
        )
        if limited_to:
            row.eligibility = "unknown"
        row_identity[id(row)] = key
        raw_rows.append(row)

    # A remote-only profile can work any listed city, so one posting listed once per city is one row.
    deduped, duplicates = dedupe_final_rows(raw_rows, merge_locations=is_remote_required(criteria))
    for row in duplicates:
        record(DiscardRecord(
            apply_url=row.apply_url, reason="duplicate_role",
            detail=f"duplicate of earlier role company={row.company} title={row.title}",
            title=row.title, company=row.company, location=row.location,
        ), row_identity[id(row)], "dedup")
    ranked = rank_final_rows(deduped, use_match_score=True)
    capped: list[FinalRow] = []
    if max_roles and len(ranked) > max_roles:
        capped = ranked[max_roles:]
        ranked = ranked[:max_roles]
    for row in capped:
        record(DiscardRecord(
            apply_url=row.apply_url, reason="final_cap", detail=f"ranked below max_roles={max_roles}",
            title=row.title, company=row.company, location=row.location,
        ), row_identity[id(row)], "final_cap")

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

    seed_kept, seed_outcomes = _seed_yield(out_dir, ranked)
    with closing(ledger.connect()) as conn:
        is_new = ledger.record_finalized_run(
            conn, summary, ranked, profile_slug=run["profile_slug"], preset=run["preset"],
            seed_kept=seed_kept, seed_outcomes=seed_outcomes,
        )

    md, js, sm = write_outputs(str(out_dir.parent), run_id, ranked, summary, discards)
    # write_outputs (ported verbatim) has no is_new notion; patch it into each results.json row.
    payload = json.loads(Path(js).read_text(encoding="utf-8"))
    for row, data in zip(ranked, payload["rows"], strict=True):
        data["is_new"] = is_new[_row_key(row)]
    Path(js).write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    finalize_discards = out_dir / "finalize_discards.jsonl"
    runs.write_jsonl(finalize_discards, stage_discards)  # atomic rewrite: idempotent, discarded.jsonl untouched

    runs.mark_stage(run_id, "finalize")
    return {
        "roles": len(ranked),
        "new_roles": sum(1 for r in ranked if is_new[_row_key(r)]),
        "unsure": sum(1 for r in ranked if r.evaluation_verdict == "unsure"),
        "discarded": len(discards),
        "final_cap": len(capped),
        "agent_evaluated": len(agent),
        "heuristic_fallback": len(current - agent.keys()),
        "outputs": {"results_md": md, "results_json": js, "run_summary_md": sm,
                    "finalize_discards_jsonl": str(finalize_discards)},
        "top": [
            {"title": r.title, "company": r.company, "match_score": r.match_score, "is_new": is_new[_row_key(r)]}
            for r in ranked[:TOP_N]
        ],
    }
