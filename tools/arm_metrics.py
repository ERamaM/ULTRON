"""Pure metrics for the ULTRON-versus-baseline arm comparison (no I/O, no network).

Every shortlist row is mapped to a pooled job id. Labels are keyed by that id, so both arms are judged
by the same blind labels. ``None`` labels are unknown and are left out of every denominator.
"""

from __future__ import annotations

from itertools import combinations
from statistics import mean, stdev
from typing import Any

LABEL_FIELDS = ("relevant", "open", "eligible", "role_fit")
COST_FIELDS = ("tokens", "tool_calls", "seconds")
AGGREGATED = (
    "rows", "unique", "duplicate_rows", "labelled", "precision", "precision_at_5", "precision_at_10",
    "closed_rate", "ineligible_rate", "role_mismatch_rate", "relevant", "pool_recall", *COST_FIELDS,
)


def ratio(numerator: float, denominator: float) -> float | None:
    """``numerator / denominator`` rounded to 4 places, or ``None`` when the denominator is 0."""
    return round(numerator / denominator, 4) if denominator else None


def jaccard(left: set[str], right: set[str]) -> float | None:
    """Jaccard similarity of two sets, ``None`` when both are empty."""
    return ratio(len(left & right), len(left | right))


def unique_in_order(ids: list[str]) -> list[str]:
    """``ids`` without repeats, first occurrence kept."""
    seen: set[str] = set()
    return [item for item in ids if not (item in seen or seen.add(item))]


def _label(labels: dict[str, dict[str, Any]], job: str, field: str) -> bool | None:
    return labels.get(job, {}).get(field)


def _share(ids: list[str], labels: dict[str, dict[str, Any]], field: str, wanted: bool) -> float | None:
    """Share of the labelled ``ids`` whose ``field`` equals ``wanted``."""
    known = [value for i in ids if (value := _label(labels, i, field)) is not None]
    return ratio(sum(1 for value in known if value is wanted), len(known))


def precision_at(ids: list[str], labels: dict[str, dict[str, Any]], k: int) -> float | None:
    """Precision of the first ``k`` ranked positions (``ids`` in rank order, repeats included).

    A position counts as a hit only for the first occurrence of a relevant job. Repeated jobs and
    empty positions (a list shorter than ``k``) are misses. Positions whose job is unlabelled are
    left out of the denominator; ``None`` when no position in the first ``k`` is labelled.
    """
    top = ids[:k]
    seen: set[str] = set()
    hits, judged, labelled_any = 0, k - len(top), False
    for job in top:
        value = _label(labels, job, "relevant")
        if value is None:
            continue
        labelled_any = True
        judged += 1
        hits += value is True and job not in seen
        seen.add(job)
    return ratio(hits, judged) if labelled_any else None


def relevant_ids(ids: list[str] | set[str], labels: dict[str, dict[str, Any]]) -> set[str]:
    """The ids labelled relevant."""
    return {i for i in ids if _label(labels, i, "relevant") is True}


def rep_metrics(
    ids: list[str], labels: dict[str, dict[str, Any]], cost: dict[str, Any], pool_relevant: set[str],
) -> dict[str, Any]:
    """Metrics of one repetition: ``ids`` are its pooled ids in rank order, already cut to the top N."""
    unique = unique_in_order(ids)
    found = relevant_ids(unique, labels)
    return {
        "rows": len(ids),
        "unique": len(unique),
        "duplicate_rows": len(ids) - len(unique),
        "labelled": sum(1 for i in unique if _label(labels, i, "relevant") is not None),
        "precision": _share(unique, labels, "relevant", True),
        "precision_at_5": precision_at(ids, labels, 5),
        "precision_at_10": precision_at(ids, labels, 10),
        "closed_rate": _share(unique, labels, "open", False),
        "ineligible_rate": _share(unique, labels, "eligible", False),
        "role_mismatch_rate": _share(unique, labels, "role_fit", False),
        "relevant": len(found),
        "pool_recall": ratio(len(found), len(pool_relevant)),
        **{field: cost.get(field) for field in COST_FIELDS},
    }


def mean_std(values: list[float | None]) -> dict[str, float | None]:
    """Mean and sample standard deviation of the known values (std ``None`` with fewer than two)."""
    known = [v for v in values if v is not None]
    if not known:
        return {"mean": None, "std": None, "n": 0}
    spread = round(stdev(known), 4) if len(known) > 1 else None
    return {"mean": round(mean(known), 4), "std": spread, "n": len(known)}


def reproducibility(rep_sets: list[set[str]]) -> float | None:
    """Mean pairwise Jaccard between the repetitions of one arm (``None`` with fewer than two)."""
    scores = [s for a, b in combinations(rep_sets, 2) if (s := jaccard(a, b)) is not None]
    return round(mean(scores), 4) if scores else None


def pooled_cost_per_relevant(per_rep: dict[str, dict[str, Any]], field: str) -> float | None:
    """Total ``field`` over the repetitions that recorded it, divided by their total relevant jobs.

    A repetition that spent its budget and found nothing still adds its cost, so a failing arm never
    looks cheaper than it is. ``None`` when no repetition recorded the cost or none found anything.
    """
    measured = [m for m in per_rep.values() if m.get(field) is not None]
    found = sum(m["relevant"] for m in measured)
    return round(sum(m[field] for m in measured) / found, 2) if found else None


def _arm_summary(
    reps: dict[str, list[str]], per_rep: dict[str, dict[str, Any]], labels: dict[str, dict[str, Any]],
    pool_relevant: set[str], others: set[str],
) -> dict[str, Any]:
    union = {i for ids in reps.values() for i in ids}
    arm_relevant = relevant_ids(union, labels)
    return {
        "repetitions": per_rep,
        "aggregate": {key: mean_std([m[key] for m in per_rep.values()]) for key in AGGREGATED},
        "cost_per_relevant": {field: pooled_cost_per_relevant(per_rep, field) for field in COST_FIELDS},
        "zero_relevant_repetitions": sum(1 for m in per_rep.values() if m["relevant"] == 0),
        "union_relevant": len(arm_relevant),
        "union_pool_recall": ratio(len(arm_relevant), len(pool_relevant)),
        "only_this_arm_relevant": len(arm_relevant - relevant_ids(others, labels)),
        "reproducibility_jaccard": reproducibility([set(ids) for ids in reps.values()]),
        "reproducibility_relevant_jaccard": reproducibility([relevant_ids(ids, labels) for ids in reps.values()]),
    }


def compare_arms(
    arms: dict[str, dict[str, list[str]]],
    labels: dict[str, dict[str, Any]],
    costs: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, Any]:
    """Full comparison: ``arms`` maps arm -> rep -> ranked pooled ids (top N), ``costs`` arm -> rep -> cost."""
    unions = {arm: {i for ids in reps.values() for i in ids} for arm, reps in arms.items()}
    everything: set[str] = set().union(*unions.values())
    pool_relevant = relevant_ids(everything, labels)
    result: dict[str, Any] = {"pool": {"jobs": len(everything), "relevant": len(pool_relevant)},
                              "arms": {}, "between_arms": {}}
    for arm, reps in sorted(arms.items()):
        per_rep = {rep: rep_metrics(ids, labels, costs.get(arm, {}).get(rep, {}), pool_relevant)
                   for rep, ids in sorted(reps.items())}
        others: set[str] = set().union(*(u for name, u in unions.items() if name != arm))
        result["arms"][arm] = _arm_summary(reps, per_rep, labels, pool_relevant, others)
    for left, right in combinations(sorted(unions), 2):
        result["between_arms"][f"{left}|{right}"] = {
            "jaccard_all": jaccard(unions[left], unions[right]),
            "jaccard_relevant": jaccard(relevant_ids(unions[left], labels), relevant_ids(unions[right], labels)),
        }
    return result
