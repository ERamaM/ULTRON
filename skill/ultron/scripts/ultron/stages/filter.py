"""``filter``: deterministic keep/discard decision plus the ambiguity queue."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from ultron.core.decision.ambiguity import AmbiguityScorer
from ultron.core.decision.unit import characterize_job, load_allowed_domains
from ultron.core.feeds.urls import is_aggregator_host
from ultron.core.models import DiscardRecord, ExtractedJob
from ultron.core.rules.geography import geo_scope, location_scope
from ultron.core.rules.hard_filters import location_excluded
from ultron.core.rules.role_matching import role_negatives_for
from ultron.core.vocabulary import active_pack
from ultron.storage import runs

# Evaluator-stage discards whose ambiguity score reaches the preset threshold go to the agent instead of
# being final, at most this many per run (highest scores first). Hard evidence is never rescued.
MAX_RESCUED_DISCARDS = 20
HARD_EVIDENCE_REASONS = frozenset({"expired_or_closed", "location_excluded", "outside_ats_allowlist"})


def allows_outside_allowlist(row: dict) -> bool:
    """MARK III's ATS-fingerprint override, at the ULTRON boundary.

    A posting whose URL came from a vendor API for a verified board (``source_type == "ats_api"``) or
    whose fetched page carries an ATS fingerprint (``ats_fingerprint``, set by ``fetch``) is not
    discarded as ``outside_ats_allowlist`` when its apply URL is on the company's own domain. The
    fingerprint never counts on an aggregator page (it is a substring match, and aggregator pages
    link to ATS boards).
    """
    candidate = row.get("candidate") or {}
    if candidate.get("source_type") == "ats_api":
        return True
    domain = str((row.get("job") or {}).get("domain") or "")
    return row.get("ats_fingerprint") is True and not is_aggregator_host(domain)


def _rescuable(result: dict, job: ExtractedJob, excluded_locations: tuple[str, ...] = ()) -> bool:
    """An evaluator-stage heuristic discard that passed every authoritative gate and has no hard evidence.

    A posting that names one of the profile's excluded locations is never rescued, even when another reason
    (seniority, say) was the one the evaluator reported: the evaluator stops at its first discard reason.
    """
    evaluation = result["_evaluation"]
    return (
        result["pipeline_characterization"]["stage"] == "heuristic_evaluator"
        and result["hard_filter"]["keep"]
        and evaluation.open_status_ok
        and evaluation.discard_category != "closed_or_invalid"
        and evaluation.discard_reason not in HARD_EVIDENCE_REASONS
        and not (
            excluded_locations
            and location_excluded(f"{job.location}\n{job.remote_text}\n{job.body_text}", excluded_locations)
        )
    )


def filter_run(run_id: str) -> dict:
    """Apply deterministic filters and ambiguity scoring to fetched postings."""
    runs.require_stage(run_id, "filter")
    run = runs.load_run(run_id)
    profile = run.get("profile_snapshot") or {}
    threshold = run["settings"]["llm_ambiguity_threshold"]
    out_dir = runs.run_dir(run_id)
    kept, ambiguous, discarded, reasons = classify_postings(
        runs.read_jsonl(out_dir / "postings.jsonl"), profile, threshold
    )
    runs.write_jsonl(out_dir / "discarded.jsonl", discarded)
    runs.write_jsonl(out_dir / "kept.jsonl", kept)
    runs.write_jsonl(out_dir / "ambiguous.jsonl", ambiguous)
    runs.mark_stage(run_id, "filter")
    return {
        "kept": len(kept),
        "ambiguous": len(ambiguous),
        "discarded": len(discarded),
        "rescued_discards": sum(1 for entry in ambiguous if "rescued_discard" in entry),
        "discard_reasons": dict(reasons.most_common()),
    }


def classify_postings(
    rows: Iterable[dict], profile: dict, threshold: float
) -> tuple[list[dict], list[dict], list[dict], Counter[str]]:
    """Split fetched posting rows into ``(kept, ambiguous, discarded, discard reasons)`` for ``profile``.

    Pure: it reads and writes nothing, so ``profile explain --run`` can replay a run with a draft profile.
    Rescued discards (``rescued_discard`` set) are part of ``ambiguous``, exactly as ``filter_run`` writes them.
    """
    allowed = load_allowed_domains()
    pack = active_pack(profile)
    geo = geo_scope(profile)
    target_regions = location_scope(profile)["preferred"]
    # Location exclusions only apply to non-European scopes, as in the heuristic evaluator.
    excluded_locations = tuple(location_scope(profile)["excluded_terms"]) if geo in {"other", "global"} else ()
    role_negatives = role_negatives_for(profile)
    scorer = AmbiguityScorer(vocabulary=pack)

    kept: list[dict] = []
    ambiguous: list[dict] = []
    discarded: list[dict] = []
    rescue_candidates: list[tuple[int, int, dict, dict]] = []
    reasons: Counter[str] = Counter()

    for order, row in enumerate(rows):
        job = ExtractedJob(**row["job"])
        identity = row["identity_key"]
        result = characterize_job(
            job, profile, allowed, vocabulary=pack, geo=geo, role_negatives=role_negatives,
            allow_outside_ats_allowlist=allows_outside_allowlist(row),
            target_regions=target_regions,
        )
        decision = result["pipeline_characterization"]
        evaluation = result["_evaluation"]
        if decision["outcome"] == "discard":
            reason = decision["reason"] or "unknown"
            record = DiscardRecord(
                apply_url=job.apply_url, reason=reason, title=job.title, company=job.company,
                location=job.location, remote_text=job.remote_text,
            ).model_dump(mode="json")
            discard = {**record, "stage": decision["stage"], "identity_key": identity}
            if _rescuable(result, job, excluded_locations):
                score = scorer.score(job, evaluation, classification_signals=result["_signals"])
                if score >= threshold:
                    entry = {
                        "identity_key": identity,
                        "job": job.model_dump(mode="json"),
                        "heuristic": evaluation.model_dump(mode="json"),
                        "ambiguity": score,
                        "rescued_discard": {"reason": reason, "stage": decision["stage"]},
                    }
                    rescue_candidates.append((score, order, entry, discard))
                    continue
            discarded.append(discard)
            reasons[reason] += 1
            continue
        score = scorer.score(job, evaluation, classification_signals=result["_signals"])
        entry = {
            "identity_key": identity,
            "job": job.model_dump(mode="json"),
            "heuristic": evaluation.model_dump(mode="json"),
            "ambiguity": score,
        }
        # Outside Europe the rules cannot judge eligibility: an unclear one goes to the agent.
        unclear_geo = geo == "other" and evaluation.eligibility is None
        if evaluation.verdict == "unsure" or score >= threshold or unclear_geo:
            ambiguous.append(entry)
        else:
            kept.append(entry)

    # Borderline evaluator-stage discards go to the agent, highest ambiguity first, at most
    # MAX_RESCUED_DISCARDS; the rest stay discarded in their original order.
    rescue_candidates.sort(key=lambda item: (-item[0], item[1]))
    ambiguous.extend(entry for _score, _order, entry, _discard in rescue_candidates[:MAX_RESCUED_DISCARDS])
    for _score, _order, _entry, discard in sorted(rescue_candidates[MAX_RESCUED_DISCARDS:], key=lambda c: c[1]):
        discarded.append(discard)
        reasons[discard["reason"]] += 1
    return kept, ambiguous, discarded, reasons
