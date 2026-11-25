"""Scout mode: the agent discovers and judges; ULTRON briefs it, expands its finds and orders its queue.

``brief`` is what ``run new --mode scout`` returns. ``expansion_seeds`` replaces the sweep of stored boards
in ``discover ats``: it queries the boards of the employers the agent found and the profile's watchlist.
``route_postings`` replaces the heuristic filter: rules only drop closed, unusable, aggregator and already
rejected postings, and every other posting goes to the agent, in a bounded queue the heuristics only order.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, get_vendor_spec, infer_vendor_from_host
from ultron.core.decision.ambiguity import AmbiguityScorer
from ultron.core.decision.unit import characterize_job
from ultron.core.discovery.url_filter import domain_of
from ultron.core.feeds.urls import is_aggregator_host
from ultron.core.models import AtsBoardSeed, CandidateUrl, ExtractedJob
from ultron.core.rules.geography import geo_scope, location_scope
from ultron.core.rules.role_matching import role_negatives_for
from ultron.core.vocabulary import active_pack
from ultron.infra import home
from ultron.profiles import store as profile_store
from ultron.seeds import load_inventory
from ultron.stages.scout_resolve import EMPLOYER_PAGE, is_recruiter
from ultron.storage import ledger, runs, scout_memory, watchlist

AGENT_SOURCES = frozenset({"web_search", EMPLOYER_PAGE})
WATCH_PASS = "watch"
EXPANSION_PASS = "expansion"
DEFAULT_QUEUE_CAP = 60
# Live finds the agent should reach before it stops searching: the size of the shortlist it is asked for.
LIVE_FINDS_TARGET = 25
CLOSED_FETCH_ERRORS = frozenset({"expired_or_closed"})
NOT_EVALUATED_FILE = "not_evaluated.jsonl"
CARRIED_FILE = "carried.jsonl"
CLOSED_REASONS = frozenset({"expired_or_closed"})
BRIEF_LIST_LIMIT = 30
_FOUND_ORDER = {"agent": 0, WATCH_PASS: 1, EXPANSION_PASS: 2}
_VERDICT_ORDER = {"keep": 0, "unsure": 1}
SEARCH_ADVICE = (
    "Search by role and region first (target titles plus remote or the regions in the profile).",
    "Follow aggregator listings (remote job boards, job search engines) to the employer's own posting.",
    "Add finds in batches with `ultron candidates add`; the reply says which are new, seen or rejected.",
    "Run `ultron fetch` after every batch: `agent_finds.live` counts your finds that are open postings.",
    ("Many search hits are stale copies of closed postings, so keep searching until agent_finds.live reaches "
    "live_finds_target (or the shortlist size the user asked for)."),
    "Skip employers in rejected_employers, and postings flagged seen or rejected.",
    "Stop early only when two searches in a row add no new employer.",
)


def queue_cap(settings: dict) -> int:
    """How many postings the agent evaluates at most in one scout run."""
    return max(1, int(settings.get("scout_queue_cap") or DEFAULT_QUEUE_CAP))


def _family(profile: dict) -> tuple[str, set[str]]:
    criteria = profile.get("criteria") or {}
    label = " ".join(str(criteria.get("role_family_label") or "").casefold().split())
    return label, {str(p).casefold() for p in criteria.get("vocabulary_packs") or [] if p}


def siblings(profile_slug: str, profile: dict) -> list[str]:
    """Other profiles of the same role family: the same role family label, or a shared vocabulary pack."""
    label, packs = _family(profile)
    out = []
    for item in profile_store.list_profiles():
        slug = str(item.get("slug") or "")
        if not slug or slug == profile_slug:
            continue
        try:
            other_label, other_packs = _family(profile_store.load_profile(slug))
        except (OSError, ValueError):
            continue
        if (label and label == other_label) or packs & other_packs:
            out.append(slug)
    return sorted(out)


def _ledger_exists() -> bool:
    return (home.ultron_home() / "ultron.sqlite3").exists()


def _profile_summary(profile: dict) -> dict[str, Any]:
    criteria = profile.get("criteria") or {}
    remote = criteria.get("remote_policy") or {}
    return {
        "name": profile.get("name", ""),
        "target_titles": list(criteria.get("target_titles") or [])[:BRIEF_LIST_LIMIT],
        "role_focus_terms": list(criteria.get("role_focus_terms") or [])[:BRIEF_LIST_LIMIT],
        "preferred_locations": list(location_scope(profile)["preferred"]),
        "remote_required": remote.get("required") if isinstance(remote, dict) else None,
        "exclusion_terms": list(criteria.get("exclusion_terms") or [])[:BRIEF_LIST_LIMIT],
    }


def _memory(profile_slug: str, profile: dict, queued: int) -> dict[str, Any]:
    if not _ledger_exists():
        return {"new_profile": True, "known_postings": 0, "watchlist": [], "rejected_employers": [],
                "pending_watch": queued}
    with closing(ledger.connect()) as conn:
        known = scout_memory.known_count(conn, profile_slug)
        watched = watchlist.watched(conn, profile_slug, siblings(profile_slug, profile))
        rejected = scout_memory.rejected_companies(conn, profile_slug, BRIEF_LIST_LIMIT)
    return {
        "new_profile": known == 0 and not watched,
        "known_postings": known,
        "watchlist": [item["company"] or item["seed_key"] for item in watched[:BRIEF_LIST_LIMIT]],
        "rejected_employers": rejected,
        "pending_watch": queued,
    }


def brief(run: dict, queued_watch: int = 0) -> dict[str, Any]:
    """The scout brief: the profile, what is already known, the budgets and search advice (no query plan)."""
    profile = run.get("profile_snapshot") or {}
    settings = run["settings"]
    return {
        "profile_summary": _profile_summary(profile),
        "memory": _memory(run["profile_slug"], profile, queued_watch),
        "budgets": {
            "max_pages": settings["max_pages"],
            "max_requests_per_host": settings["max_requests_per_host"],
            "max_requests_per_vendor": settings["max_requests_per_vendor"],
            "boards_to_expand": settings["max_seeds_per_run"],
            "evaluation_queue": queue_cap(settings),
            "live_finds_target": LIVE_FINDS_TARGET,
        },
        "advice": list(SEARCH_ADVICE),
    }


def queue_pending_watch(run_id: str, run: dict) -> int:
    """Add the postings ``ultron watch`` left pending for this profile to the run, first in its queue."""
    if not _ledger_exists():
        return 0
    with closing(ledger.connect()) as conn:
        pending = watchlist.pending(conn, run["profile_slug"])
        if not pending:
            return 0
        rows = [CandidateUrl.model_validate({**item, "search_pass": WATCH_PASS}) for item in pending]
        runs.append_jsonl(runs.run_dir(run_id) / "candidates.jsonl", rows)
        keys = [str(item.get("identity_key") or item.get("normalized_url") or "") for item in pending]
        watchlist.mark_queued(conn, run["profile_slug"], keys, run_id)
    runs.mark_stage(run_id, "candidates")  # the run can fetch its watched postings without any agent find
    return len(rows)


# --- incremental fetch ----------------------------------------------------------------------------


def pending_fetches(out_dir, unique: list) -> tuple[list, list[dict], list[dict]]:
    """Candidates a scout run has not fetched yet, and the postings and errors already recorded.

    ``unique`` holds ``(candidate, identity)`` pairs. A candidate counts as fetched when its identity has a
    posting or its URL has a fetch error, so repeated fetches between batches of finds never refetch it.
    """
    postings = runs.read_jsonl(out_dir / "postings.jsonl")
    errors = runs.read_jsonl(out_dir / "fetch_errors.jsonl")
    done_keys = {row.get("identity_key") for row in postings}
    done_urls = {row.get("url") for row in errors} | {
        (row.get("candidate") or {}).get("normalized_url") for row in postings
    }
    todo = [(candidate, key) for candidate, key in unique
            if key not in done_keys and candidate.normalized_url not in done_urls]
    return todo, postings, errors


def agent_find_counts(out_dir, postings: list[dict], errors: list[dict]) -> dict[str, int]:
    """How the agent's own finds fared at fetch: open postings, closed ones and other failures."""
    finds = {row.get("normalized_url") for row in runs.read_jsonl(out_dir / "candidates.jsonl")
             if found_by(row) == "agent"}
    live = {(row.get("candidate") or {}).get("normalized_url") for row in postings
            if found_by(row.get("candidate")) == "agent"}
    closed = failed = 0
    for row in errors:
        if row.get("url") not in finds:
            continue
        if str(row.get("error") or "").split(":")[0] in CLOSED_FETCH_ERRORS:
            closed += 1
        else:
            failed += 1
    return {"live": len(live), "closed": closed, "failed": failed, "target": LIVE_FINDS_TARGET}


# --- expansion ------------------------------------------------------------------------------------


def found_boards(out_dir) -> list:
    """Supported ATS boards of the postings the agent found in this run, in the order they were found."""
    boards: dict[tuple[str, str], Any] = {}
    for row in runs.read_jsonl(out_dir / "candidates.jsonl"):
        if row.get("source_type") not in AGENT_SOURCES or is_recruiter(row.get("ats_company") or ""):
            continue
        url = str(row.get("source_url") or row.get("normalized_url") or "")
        if not infer_vendor_from_host(domain_of(url)):
            continue
        seed = extract_seed_from_url(url, row.get("ats_company") or row.get("company_hint") or "")
        if seed is not None:
            boards.setdefault(seed_identity(seed.ats, seed.board_identifier), seed)
    return list(boards.values())


def watch_seeds(run: dict) -> list:
    """The inventory seeds of the profile's watchlist (user-added boards, then boards with past keeps)."""
    if not _ledger_exists():
        return []
    profile = run.get("profile_snapshot") or {}
    try:
        with closing(ledger.connect()) as conn:
            watched = watchlist.watched(conn, run["profile_slug"], siblings(run["profile_slug"], profile))
    except sqlite3.Error:
        return []
    inventory = {":".join(seed_identity(s.ats, s.board_identifier)): s for s in load_inventory(None)}
    out = []
    for item in watched:
        seed = inventory.get(item["seed_key"]) or board_seed(item["seed_key"], item["company"])
        if seed is not None:
            out.append(seed)
    return out


def board_seed(key: str, company: str = ""):
    """A board seed for a ``vendor:board`` key outside the inventory, or ``None`` for an unknown vendor."""
    vendor, _, board = key.partition(":")
    if get_vendor_spec(vendor) is None or not board:
        return None
    try:
        return AtsBoardSeed(seed_id=f"api_{vendor}_{board}", company=company or board, ats=vendor,
                            board_identifier=board, enabled=True, priority=200, tags=["watchlist"])
    except ValueError:
        return None


def expansion_seeds(run: dict, out_dir, request_host) -> tuple[list[tuple[Any, str]], dict[str, dict[str, int]]]:
    """The boards a scout run queries: found employers first, then the watchlist, capped by the preset.

    ``request_host`` maps a seed to the host its API requests go to, for the per-host cap. Returns the
    ``(seed, group)`` pairs in query order and per-group counts like ``discover ats`` reports them.
    """
    settings = run["settings"]
    limit, per_host = settings["max_seeds_per_run"], max(1, settings["max_requests_per_host"])
    groups = {g: {"queried": 0, "failed": 0, "skipped": 0, "host_capped": 0, "capped": 0} for g in ("found", "watchlist")}
    chosen: list[tuple[Any, str]] = []
    seen: set[tuple[str, str]] = set()
    hosts: dict[str, int] = {}
    for group, seeds in (("found", found_boards(out_dir)), ("watchlist", watch_seeds(run))):
        for seed in seeds:
            key = seed_identity(seed.ats, seed.board_identifier)
            if key in seen:
                continue
            seen.add(key)
            host = request_host(seed)
            if hosts.get(host, 0) >= per_host:
                groups[group]["host_capped"] += 1
            elif len(chosen) >= limit:
                groups[group]["capped"] += 1
            else:
                hosts[host] = hosts.get(host, 0) + 1
                chosen.append((seed, group))
    return chosen, groups


# --- routing --------------------------------------------------------------------------------------


def found_by(candidate: dict | None) -> str:
    """Who brought a posting into the run: the agent, the watch, or expansion of a board."""
    candidate = candidate or {}
    if candidate.get("search_pass") == WATCH_PASS:
        return WATCH_PASS
    if candidate.get("source_type") in AGENT_SOURCES:
        return "agent"
    return EXPANSION_PASS


def _hard_drop(job: ExtractedJob, result: dict) -> str | None:
    """The reason a scout run drops a posting without the agent, or ``None``."""
    if not job.title or not (job.body_text or job.location):
        return "unusable_page"
    if is_aggregator_host(job.domain):
        return "aggregator_page"
    reasons = {result["pre_filter"]["reason"], result["hard_filter"]["reason"]}
    if reasons & CLOSED_REASONS or result["_evaluation"].discard_category == "closed_or_invalid":
        return "expired_or_closed"
    return None


def _memory_lookup(profile_slug: str, keys: list[str]) -> tuple[dict[str, str], Any]:
    if not _ledger_exists() or not keys:
        return {key: "new" for key in keys}, None
    conn = ledger.connect()
    return scout_memory.flags(conn, profile_slug, keys), conn


def route_postings(rows: list[dict], run: dict) -> dict[str, Any]:
    """Split fetched postings into the agent's queue, carried rows, postings beyond the cap and drops.

    Returns ``queue`` (in evaluation order), ``not_evaluated``, ``carried`` (seen postings with unchanged
    facts and their earlier row), ``discarded`` and ``reasons``.
    """
    profile = run.get("profile_snapshot") or {}
    pack, geo = active_pack(profile), geo_scope(profile)
    scorer = AmbiguityScorer(vocabulary=pack)
    negatives = role_negatives_for(profile)
    regions = location_scope(profile)["preferred"]
    flags, conn = _memory_lookup(run["profile_slug"], [row["identity_key"] for row in rows])
    criteria_digest = scout_memory.profile_hash(profile)
    queue: list[tuple[tuple, dict]] = []
    carried: list[dict] = []
    discarded: list[dict] = []
    reasons: dict[str, int] = {}

    def drop(job: ExtractedJob, key: str, reason: str) -> None:
        discarded.append({"apply_url": job.apply_url, "reason": reason, "detail": "", "title": job.title,
                          "company": job.company, "location": job.location, "remote_text": job.remote_text,
                          "stage": "scout_filter", "identity_key": key})
        reasons[reason] = reasons.get(reason, 0) + 1

    try:
        for order, row in enumerate(rows):
            job, key = ExtractedJob(**row["job"]), row["identity_key"]
            result = characterize_job(job, profile, [], vocabulary=pack, geo=geo, role_negatives=negatives,
                                      allow_outside_ats_allowlist=True, target_regions=regions)
            if reason := _hard_drop(job, result):
                drop(job, key, reason)
                continue
            flag = flags.get(key, "new")
            if flag in {"rejected", "applied"}:
                drop(job, key, f"already_{flag}")
                continue
            digest = scout_memory.content_hash(job.title, job.location, job.remote_text, job.body_text)
            if flag == "seen" and conn is not None:
                previous = scout_memory.carried_row(conn, run["profile_slug"], key, digest, criteria_digest)
                if previous is not None:
                    carried.append({"identity_key": key, "row": previous[0].model_dump(mode="json"),
                                    "priority": previous[1]})
                    continue
            evaluation = result["_evaluation"]
            source = found_by(row.get("candidate"))
            hint = result["pipeline_characterization"]
            entry = {
                "identity_key": key, "job": job.model_dump(mode="json"),
                "heuristic": evaluation.model_dump(mode="json"),
                "ambiguity": scorer.score(job, evaluation, classification_signals=result["_signals"]),
                "found_by": source, "memory": "changed" if flag == "seen" else flag, "content_hash": digest,
                "hint": {"outcome": hint["outcome"], "reason": hint["reason"], "stage": hint["stage"]},
            }
            rank = (_FOUND_ORDER[source], 0 if hint["outcome"] != "discard" else 1,
                    _VERDICT_ORDER.get(evaluation.verdict, 2), -entry["ambiguity"], order)
            queue.append((rank, entry))
    finally:
        if conn is not None:
            conn.close()
    ordered = [entry for _rank, entry in sorted(queue, key=lambda item: item[0])]
    cap = queue_cap(run["settings"])
    return {"queue": ordered[:cap], "not_evaluated": ordered[cap:], "carried": carried,
            "discarded": discarded, "reasons": reasons}
