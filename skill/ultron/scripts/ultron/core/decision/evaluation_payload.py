"""Production merge semantics for explicit model evidence and absent observations."""

from ultron.core.models import JobEvaluation

# These nulls are explicit verdicts; other nulls mean the model did not say.
NULL_MEANINGFUL_FIELDS = frozenset({"eligibility", "discard_category", "discard_reason"})


def merge_evaluation_payload(payload: dict, fallback: JobEvaluation) -> dict:
    base = fallback.model_dump()
    if not isinstance(payload, dict):
        return base
    allowed = set(base)
    for key, value in payload.items():
        if key not in allowed:
            continue
        if value is None and key not in NULL_MEANINGFUL_FIELDS:
            continue
        base[key] = value
    return base
