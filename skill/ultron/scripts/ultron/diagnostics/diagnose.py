"""Missed-job diagnosis: which stage dropped a job URL, from run artifacts or an offline replay."""

from __future__ import annotations

import json
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from ultron.core import presets
from ultron.core.ats.clients.occupop import occupop_job_ref
from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.extractors import parse_ats_page
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import CandidateUrl, JobRawPage
from ultron.core.rules.geography import location_scope
from ultron.infra.net import HttpError, RequestBudgetExceeded, URLRejected
from ultron.profiles import store as profiles
from ultron.stages.feeds import is_aggregator_host
from ultron.stages.fetch import (
    get_posting_page,
    has_ats_fingerprint,
    occupop_posting_job,
    page_canonical_url,
    resolve_apply_url,
)
from ultron.stages.http_factory import client_for_run
from ultron.storage import ledger, runs

FILTER_STAGES = {"pre_filter": "prefilter", "heuristic_evaluator": "evaluator", "hard_filter": "hard_filter"}
FINALIZE_STAGES = {
    "final_cap": "final_cap", "dedup": "dedup", "evaluator": "evaluator", "post_eval_geo": "hard_filter",
}


@dataclass
class Diagnosis:
    """Diagnostic trace identifying where and why a submitted URL failed."""
    submitted_url: str
    identity_key: str
    first_failed_stage: str
    reason_code: str
    trace: list[dict] = field(default_factory=list)
    run_id: str | None = None


def _pick_run(profile_slug: str, run_id: str | None) -> str | None:
    if run_id:
        runs.load_run(run_id)  # raises RunNotFoundError
        return run_id
    for row in runs.list_runs():  # newest first
        if row.get("profile_slug") == profile_slug:
            return row["run_id"]
    return None


def _identity_of(url: str, source_type: str = "", source_key: str = "") -> str:
    return canonical_job_identity(url, source_type=source_type, source_key=source_key).identity_key


def _candidate_identities(row: dict) -> set[str]:
    url = row.get("source_url") or row.get("normalized_url") or ""
    return {_identity_of(url, row.get("source_type") or "", row.get("source_key") or ""), _identity_of(url)}


def _step(trace: list[dict], stage: str, passed: bool, reason: str = "", label: str = "run") -> None:
    trace.append({"stage": stage, "label": label, "passed": passed, "reason": reason})


def _posting_aliases(out_dir) -> dict[str, str]:
    """Identity of each posting's final apply URL -> the posting's (candidate) identity key.

    Fetch can replace a custom-domain URL with the ATS canonical URL; results then carry the new URL while the
    candidate and every stage artifact keep the original identity.
    """
    aliases: dict[str, str] = {}
    for row in runs.read_jsonl(out_dir / "postings.jsonl"):
        key = row.get("identity_key")
        apply_url = (row.get("job") or {}).get("apply_url") or ""
        if key and apply_url:
            aliases.setdefault(_identity_of(apply_url), key)
    return aliases


def _lookup(run_id: str, identity: str, trace: list[dict]) -> tuple[str, str]:
    out_dir = runs.run_dir(run_id)
    stages_done = runs.load_run(run_id).get("stages_done") or []
    aliases = _posting_aliases(out_dir)
    wanted = {identity}
    if identity in aliases:  # the submitted URL is the canonical URL fetch switched to
        wanted.add(aliases[identity])

    cand_ids: dict[str, set[str]] = {}
    matched: set[str] = set()
    for row in runs.read_jsonl(out_dir / "candidates.jsonl"):
        ids = _candidate_identities(row)
        for key in (row.get("source_url"), row.get("normalized_url")):
            if key:
                cand_ids[key] = ids
        if ids & wanted:
            matched |= ids
    if not matched:
        _step(trace, "candidates", False, "not_discovered")
        return "not_discovered", "not_discovered"
    wanted |= matched
    _step(trace, "candidates", True)

    for row in runs.read_jsonl(out_dir / "fetch_errors.jsonl"):
        url = row.get("url") or ""
        if (cand_ids.get(url) or {_identity_of(url)}) & wanted:
            error = str(row.get("error") or "")
            _step(trace, "fetch", False, error)
            return ("extraction_failed" if "extract" in error else "fetch_failed"), error
    if "fetch" not in stages_done:
        _step(trace, "fetch", False, "not_fetched")
        return "pending", "not_fetched"
    if not ({r.get("identity_key") for r in runs.read_jsonl(out_dir / "postings.jsonl")} & wanted):
        capped_path = out_dir / "fetch_capped.jsonl"
        # Runs from before fetch recorded capped candidates: not fetched and not failed can only mean the cap.
        if not capped_path.is_file() or {r.get("identity_key") for r in runs.read_jsonl(capped_path)} & wanted:
            _step(trace, "fetch", False, "candidate_cap")
            return "candidate_cap", "candidate_cap"
        _step(trace, "fetch", False, "not_fetched")
        return "pending", "not_fetched"
    _step(trace, "fetch", True)

    for row in runs.read_jsonl(out_dir / "discarded.jsonl"):
        if row.get("identity_key") in wanted:
            reason = str(row.get("reason") or "")
            _step(trace, "filter", False, reason)
            return FILTER_STAGES.get(str(row.get("stage")), "evaluator"), reason
    _step(trace, "filter", True)

    for row in runs.read_jsonl(out_dir / "finalize_discards.jsonl"):
        if row.get("identity_key") in wanted:
            reason = str(row.get("reason") or "")
            _step(trace, "finalize", False, reason)
            return FINALIZE_STAGES.get(str(row.get("stage")), "evaluator"), reason

    results = out_dir / "results.json"
    if results.is_file():
        try:
            rows = json.loads(results.read_text(encoding="utf-8")).get("rows") or []
        except ValueError:
            rows = []
        for row in rows:
            apply_url = row.get("apply_url") or ""
            key = _identity_of(apply_url, row.get("source_type") or "", row.get("source_key") or "")
            if key in wanted or aliases.get(_identity_of(apply_url)) in wanted:
                _step(trace, "finalize", True)
                return "kept", "kept"
    in_ambiguous = any(r.get("identity_key") in wanted for r in runs.read_jsonl(out_dir / "ambiguous.jsonl"))
    in_kept = any(r.get("identity_key") in wanted for r in runs.read_jsonl(out_dir / "kept.jsonl"))
    if in_ambiguous or in_kept:
        reason = "pending_evaluation" if in_ambiguous else "not_finalized"
        _step(trace, "finalize", False, reason)
        return "pending", reason
    _step(trace, "finalize", False, "not_found_in_results")
    return "pending", "not_finalized"


def _replay(url: str, profile: dict, run: dict | None, profile_slug: str, trace: list[dict]) -> None:
    label = "offline_replay"
    run = run or {"settings": presets.resolve("balanced"), "profile_slug": profile_slug}
    client = client_for_run(run)
    try:
        status, final_url, html = get_posting_page(client, url)  # an Occupop link reads the vendor's job JSON
    except (HttpError, URLRejected, RequestBudgetExceeded) as exc:
        _step(trace, "fetch", False, f"{type(exc).__name__}: {exc}", label)
        return
    if status >= 400:
        _step(trace, "fetch", False, "expired_or_closed" if status in {404, 410} else "inaccessible", label)
        return
    allowed_domains = load_allowed_domains()
    if occupop_job_ref(url):
        job, reason = occupop_posting_job(url, html)
        if job is None:
            _step(trace, "fetch", False, reason, label)
            return
        _step(trace, "fetch", True, label=label)
        fingerprint = True
    else:
        _step(trace, "fetch", True, label=label)
        page = JobRawPage(url=url, final_url=final_url, status_code=status, html=html,
                          canonical_url=page_canonical_url(html, final_url))
        try:
            job = parse_ats_page(page, ats_hints={})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            _step(trace, "extraction", False, f"extract_failed: {exc!r}", label)
            return
        submitted = CandidateUrl(query_id="diagnose", query="", source_url=url, normalized_url=normalize_url(url),
                                 domain=domain_of(normalize_url(url)))
        job.apply_url = resolve_apply_url(submitted, final_url, page.canonical_url, allowed_domains)
        job.domain = domain_of(job.apply_url)
        fingerprint = has_ats_fingerprint(html)
    _step(trace, "extraction", True, label=label)
    # Same override as the filter stage; the replay has no candidate, so only the page fingerprint counts.
    result = characterize_job(
        job, profile, allowed_domains,
        allow_outside_ats_allowlist=fingerprint and not is_aggregator_host(job.domain),
        target_regions=location_scope(profile)["preferred"],
    )
    pre = result["pre_filter"]
    _step(trace, "pre_filter", bool(pre["keep"]), str(pre["reason"] or ""), label)
    ev = result["heuristic_evaluator"]
    _step(trace, "heuristic_evaluator", bool(ev["keep"]), str(ev["discard_reason"] or ""), label)
    hard = result["hard_filter"]
    _step(trace, "hard_filter", bool(hard["keep"]), str(hard["reason"] or ""), label)
    decision = result["pipeline_characterization"]
    trace.append({"stage": "pipeline", "label": label, "passed": decision["outcome"] != "discard",
                  "reason": str(decision.get("reason") or ""), "would_fail_at": decision.get("stage")})


def diagnose(url: str, *, profile_slug: str, run_id: str | None = None, fetch: bool = True) -> Diagnosis:
    """Replay a single job URL through pipeline stages to explain its outcome."""
    identity = _identity_of(url)
    chosen = _pick_run(profile_slug, run_id)
    trace: list[dict] = []
    run = runs.load_run(chosen) if chosen else None
    if chosen:
        stage, reason = _lookup(chosen, identity, trace)
    else:
        stage, reason = "not_discovered", "no_run"
        _step(trace, "run", False, "no_run")
    if stage == "not_discovered" and fetch:
        profile = (run or {}).get("profile_snapshot") or profiles.load_profile(profile_slug)
        _replay(url, profile, run, profile_slug, trace)
    diagnosis = Diagnosis(url, identity, stage, reason, trace, chosen)
    with closing(ledger.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO diagnostics (submitted_url, identity_key, first_failed_stage, reason_code, trace_json,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (url, identity, stage, reason, json.dumps({"run_id": chosen, "trace": trace}, ensure_ascii=False),
             datetime.now(UTC).isoformat()),
        )
    return diagnosis


def to_dict(diagnosis: Diagnosis) -> dict:
    """Serialize a ``Diagnosis`` dataclass to a plain dictionary."""
    return asdict(diagnosis)
