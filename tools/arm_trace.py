"""Miss trace for an ULTRON arm: what its runs did with each relevant job the arm did not export.

For every relevant pooled job that no first-phase repetition of the arm put in its top N, each repetition's
run folder is searched for the job, from the last stage back to the first. The first match gives the
outcome: exported below the cut, discarded (with stage and reason), not evaluated, rejected by the agent,
fetched with an error, a candidate that never became a posting, an employer that was queried without
listing the job, or an employer that was never queried.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from arm_inputs import ComparisonError
from replay_run import ReplayError, read_run_files

from ultron.core.job_identity import canonical_job_identity
from ultron.storage.runs import jsonl_lines

TRACE_FILES = ("candidates.jsonl", "not_evaluated.jsonl", "fetch_errors.jsonl", "seed_attempts.jsonl",
               "board_report.jsonl")


def _identity(url: str) -> str:
    return canonical_job_identity(url or "").identity_key


def _fold(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


def _jsonl(text: str | None) -> list[dict[str, Any]]:
    rows = []
    for line in jsonl_lines(text or ""):
        if line.strip():
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _url_of(row: dict[str, Any]) -> str:
    job = row.get("job") if isinstance(row.get("job"), dict) else {}
    candidate = row.get("candidate") if isinstance(row.get("candidate"), dict) else {}
    return str(row.get("apply_url") or job.get("apply_url") or row.get("normalized_url") or row.get("url")
               or candidate.get("normalized_url") or "")


def _keys(rows: list[dict[str, Any]]) -> set[str]:
    keys = set()
    for row in rows:
        if row.get("identity_key"):
            keys.add(str(row["identity_key"]))
        if url := _url_of(row):
            keys.add(_identity(url))
    return keys


class RunView:
    """The parts of one run folder the trace needs, indexed by canonical identity."""

    def __init__(self, texts: dict[str, str | None]) -> None:
        results = json.loads(texts.get("results.json") or "{}")
        self.exported = {_identity(str(r.get("apply_url") or "")): rank
                         for rank, r in enumerate(results.get("rows") or [], start=1)}
        self.discards: dict[str, str] = {}
        for name in ("discarded.jsonl", "finalize_discards.jsonl"):
            for row in _jsonl(texts.get(name)):
                label = f"discarded:{row.get('stage') or 'unknown'}:{row.get('reason') or 'unknown'}"
                for key in _keys([row]):
                    self.discards[key] = label
        self.not_evaluated = _keys(_jsonl(texts.get("not_evaluated.jsonl")))
        self.queued = _keys(_jsonl(texts.get("ambiguous.jsonl")) + _jsonl(texts.get("kept.jsonl")))
        self.rejected = {str(r.get("identity_key")) for r in _jsonl(texts.get("evaluations.jsonl"))
                         if not (r.get("evaluation") or {}).get("keep")}
        self.fetch_failed = _keys(_jsonl(texts.get("fetch_errors.jsonl")))
        self.postings = _keys(_jsonl(texts.get("postings.jsonl")))
        candidates = _jsonl(texts.get("candidates.jsonl"))
        self.candidates = _keys(candidates)
        # Employers whose board ``discover ats`` actually queried: the board report, else the ATS candidates.
        boards = _jsonl(texts.get("board_report.jsonl"))
        queried = boards or [c for c in candidates if c.get("source_type") == "ats_api"]
        self.companies = {_fold(r.get("company") or r.get("ats_company")) for r in queried} - {""}

    def outcome(self, keys: set[str], company: str, top_n: int) -> str:
        """The furthest stage the job reached in this run."""
        if any(key in self.exported for key in keys):
            return "exported_below_top_n" if min(self.exported[k] for k in keys if k in self.exported) > top_n \
                else "exported"
        for key in keys:
            if key in self.discards:
                return self.discards[key]
        checks = (
            (self.not_evaluated, "not_evaluated"), (self.rejected, "evaluated_rejected"),
            (self.queued, "not_evaluated"), (self.postings, "posting_not_queued"),
            (self.fetch_failed, "fetch_failed"), (self.candidates, "candidate_not_fetched"),
        )
        for found, label in checks:
            if keys & found:
                return label
        return "employer_queried_job_missing" if _fold(company) in self.companies else "employer_never_queried"


def load_run_view(run_id: str, ultron_home: str | None) -> RunView:
    """The trace view of ``run_id`` under ``ultron_home`` (the home the run was ingested from)."""
    try:
        return RunView(read_run_files(run_id, extra=TRACE_FILES, ultron_home=ultron_home))
    except (ReplayError, ValueError) as exc:
        raise ComparisonError(f"run {run_id} cannot be traced: {exc}") from exc


def trace_arm(
    state: dict[str, Any], arm: str, pooled: dict[str, Any], labels: dict[str, dict[str, Any]],
    views: dict[str, RunView],
) -> dict[str, Any]:
    """Outcomes of every relevant pooled job ``arm`` missed, per repetition of ``arm``.

    ``views`` maps the arm's repetitions to their run views; the arm's top-N rows come from ``pooled``.
    """
    top_n = state["top_n"]
    exported = {job for (key, _rep, _i), job in pooled["rows"].items() if key == arm}
    relevant = {job for job, label in labels.items() if label.get("relevant") is True and job in pooled["jobs"]}
    totals: Counter[str] = Counter()
    jobs = []
    for job in sorted(relevant - exported):
        info = pooled["jobs"][job]
        keys = {_identity(v["apply_url"]) for v in info["variants"]}
        outcomes = {rep: view.outcome(keys, info["company"], top_n) for rep, view in sorted(views.items())}
        totals.update(outcomes.values())
        jobs.append({"id": job, "company": info["company"], "title": info["title"], "outcomes": outcomes})
    return {"arm": arm, "relevant_missed": len(jobs), "outcome_counts": dict(totals.most_common()), "jobs": jobs}
