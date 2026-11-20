"""``ultron profile explain``: what a profile makes the deterministic pipeline do, built from the real rules.

Everything here calls the same functions the stages call (role terms, title vocabulary, geography scope,
remote requirement, query plan, filter), so the explanation cannot drift from the engine. ``--run``
replays the filter of an earlier run with the profile, without writing anything.
"""

from __future__ import annotations

from collections import Counter

from ultron import presets, runs
from ultron.core.decision.unit import _role_terms
from ultron.core.discovery.title_vocabulary import build_title_vocabulary
from ultron.core.remote_requirement import is_remote_required, remote_required_value
from ultron.core.rules.geography import geo_scope, location_scope, scoped_eligibility_terms
from ultron.core.rules.term_matching import normalize_text
from ultron.core.vocabulary import active_pack
from ultron.runs_cmd import build_query_plan

MAX_EXAMPLES = 5
_SCOPE_EFFECT = {
    "eu": "built-in Spain/EU/Europe remote and eligibility rules apply; UK-only, US-only and similar are out of scope",
    "other": "no European rules; eligibility comes from the profile's terms and unclear postings go to the agent",
    "global": "no preferred region; eligibility comes only from the profile's terms",
}


def _list(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _norm(term: str) -> str:
    return " ".join(normalize_text(term).split())


def _role_source(criteria: dict, pack_has_terms: bool) -> str:
    if _list(criteria.get("role_focus_terms")):
        return "role_focus_terms" if pack_has_terms else "role_focus_terms + target_titles"
    return "vocabulary pack" if pack_has_terms else "target_titles"


def _warnings(profile: dict, criteria: dict, ignored_exclusions: list[str], plan: list[str]) -> list[str]:
    out: list[str] = []
    for path, values in _term_fields(profile):
        seen: dict[str, str] = {}
        for term in values:
            key = _norm(term)
            if key in seen:
                out.append(f"{path}: {term!r} duplicates {seen[key]!r}")
            seen.setdefault(key, term)
            if any(sep in term for sep in ",;|"):
                out.append(f"{path}: {term!r} looks like several terms in one; list them separately")
            elif len(term.split()) > 6:
                out.append(f"{path}: {term!r} is long; terms match as exact phrases, so short terms match more")
    for term in ignored_exclusions:
        out.append(f"exclusion {term!r} is ignored for titles because it overlaps the profile's own role terms")
    focus, titles = _list(criteria.get("role_focus_terms")), _list(criteria.get("target_titles"))
    if len(focus) > 12:
        out.append(f"criteria.role_focus_terms has {len(focus)} terms; 5-12 focused terms work best")
    if len(titles) > 6:
        out.append(f"criteria.target_titles has {len(titles)} titles; 3-6 work best")
    preferred = location_scope(profile)["preferred"]
    regions = _list((profile.get("query_templates") or {}).get("regions"))
    if preferred and not regions:
        out.append("query_templates.regions is empty: web-search queries will not name any of the preferred regions")
    if not plan:
        out.append("the query plan is empty: add criteria.target_titles or query_templates.core_titles")
    return out


def _term_fields(profile: dict) -> list[tuple[str, list[str]]]:
    fields: list[tuple[str, list[str]]] = []

    def walk(node: object, prefix: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{prefix}.{key}" if prefix else str(key))
        elif isinstance(node, list) and all(isinstance(item, str) for item in node):
            fields.append((prefix, node))

    walk(profile.get("criteria"), "criteria")
    walk(profile.get("query_templates"), "query_templates")
    return fields


def explain(profile: dict, *, preset: str = presets.DEFAULT_PRESET) -> dict:
    """A structured summary of the filters, signals and queries ``profile`` produces (profile must be valid)."""
    criteria = profile.get("criteria") or {}
    pack = active_pack(profile)
    seniority = criteria.get("seniority") if isinstance(criteria.get("seniority"), dict) else {}
    policy = criteria.get("remote_policy") if isinstance(criteria.get("remote_policy"), dict) else {}
    scope = location_scope(profile)
    geo = geo_scope(profile)
    profile_negatives = [*_list(criteria.get("exclusion_terms")), *_list(seniority.get("excluded_terms"))]
    title_vocabulary = build_title_vocabulary(criteria, vocabulary=pack)
    applied = {_norm(term) for term in title_vocabulary.excluded_terms}
    ignored = [term for term in dict.fromkeys(profile_negatives) if _norm(term) not in applied]
    settings = presets.resolve(preset)
    plan = build_query_plan(profile, settings["max_search_queries"])
    return {
        "slug": profile.get("slug"),
        "name": profile.get("name"),
        "role_match": {
            "effect": "strict: a posting must match one of these role terms (title synonyms included)",
            "source": _role_source(criteria, bool(pack.role_terms)),
            "terms": list(_role_terms(profile, pack)) or list(pack.role_terms),
            "vocabulary_packs": _list(criteria.get("vocabulary_packs")),
            "target_titles": _list(criteria.get("target_titles")),
            "body_signal_terms": _list(criteria.get("role_signal_terms")),
        },
        "seniority": {
            "effect": "the filter checks the level: preferred_terms mark the wanted level and excluded_terms an "
                      "unwanted one (it can discard the posting); with required false, only excluded_terms count",
            "required": bool(seniority.get("required", True)),
            "preferred_terms": _list(seniority.get("preferred_terms")),
            "excluded_terms": _list(seniority.get("excluded_terms")),
        },
        "remote": {
            "required": remote_required_value(criteria),
            "remote_only": is_remote_required(criteria),
            "positive_terms": _list(policy.get("positive_terms")),
            "negative_terms": _list(policy.get("negative_terms")),
        },
        "geography": {
            "scope": geo,
            "effect": _SCOPE_EFFECT[geo],
            "preferred": list(scope["preferred"]),
            "eligibility_terms": list(scoped_eligibility_terms(profile)) if geo != "eu" else list(
                scope["eligibility_terms"]),
            "excluded_terms": list(scope["excluded_terms"]),
        },
        "title_exclusions": {
            "effect": "titles containing these (exclusion_terms and seniority.excluded_terms) are skipped when "
                      "job boards and feeds are listed; afterwards exclusion_terms only lower the score, while "
                      "seniority.excluded_terms keep marking a posting as the wrong level",
            "applied": [term for term in dict.fromkeys(profile_negatives) if _norm(term) in applied],
            "ignored": ignored,
        },
        "fit_signals": {
            "effect": "shown as fit signals and used in ranking; they never reject a posting",
            "specialties": _list(criteria.get("specialties")) or _list(
                (profile.get("query_templates") or {}).get("specialties")),
        },
        "query_plan": {"preset": preset, "max_queries": settings["max_search_queries"], "queries": plan},
        "agent_notes": profile.get("prompt") if isinstance(profile.get("prompt"), str) else "",
        "warnings": _warnings(profile, criteria, ignored, plan),
    }


def _outcomes(run_id: str) -> dict[str, str]:
    folder = runs.run_dir(run_id)
    outcome: dict[str, str] = {}
    for name in ("kept", "ambiguous", "discarded"):
        for row in runs.read_jsonl(folder / f"{name}.jsonl"):
            if isinstance(row, dict) and row.get("identity_key"):
                outcome[str(row["identity_key"])] = name
    return outcome


def _example(row: dict, before: str | None, after: str, reason: str | None) -> dict:
    job = row.get("job") or {}
    entry = {"title": job.get("title"), "company": job.get("company"), "before": before, "after": after}
    if reason:
        entry["reason"] = reason
    return entry


def replay_run(profile: dict, run_id: str) -> dict:
    """Run the filter stage of ``run_id``'s fetched postings with ``profile``; compare with the run's own result."""
    from ultron.stages.filter import classify_postings

    run = runs.load_run(run_id)
    if "fetch" not in run.get("stages_done", []):
        raise runs.StageOrderError(f"run {run_id} has no fetched postings yet (run `ultron fetch` first)")
    rows = runs.read_jsonl(runs.run_dir(run_id) / "postings.jsonl")
    kept, ambiguous, discarded, reasons = classify_postings(rows, profile, run["settings"]["llm_ambiguity_threshold"])
    after: dict[str, tuple[str, str | None]] = {}
    for name, items in (("kept", kept), ("ambiguous", ambiguous), ("discarded", discarded)):
        for item in items:
            after[str(item["identity_key"])] = (name, item.get("reason"))
    before = _outcomes(run_id) if "filter" in run.get("stages_done", []) else {}
    transitions: Counter[str] = Counter()
    examples: dict[str, list[dict]] = {}
    for row in rows:
        key = str(row.get("identity_key"))
        new, reason = after.get(key, ("discarded", None))
        old = before.get(key)
        if before and old != new:
            label = f"{old or 'unknown'} -> {new}"
            transitions[label] += 1
            bucket = examples.setdefault(label, [])
            if len(bucket) < MAX_EXAMPLES:
                bucket.append(_example(row, old, new, reason))
    return {
        "run_id": run_id,
        "run_profile": run.get("profile_slug"),
        "postings": len(rows),
        "kept": len(kept),
        "ambiguous": len(ambiguous),
        "discarded": len(discarded),
        "discard_reasons": dict(reasons.most_common()),
        "compared_with_run": bool(before),
        "changes": dict(transitions.most_common()),
        "examples": examples,
        "note": "only the filter stage is replayed: postings the run never discovered or fetched cannot appear",
    }
