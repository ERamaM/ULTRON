"""Read optional toolkit records and project them into sanitized web export fields."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from typing import Any, get_args

from ultron.core.extractors.posting_date import iso_vacancy_date
from ultron.core.models import Eligibility, RemotePolicy
from ultron.exports import sanitize, view_model
from ultron.storage.watchlist import watched

RELEVANCE = ("relevant", "not_relevant", "uncertain")


def json_object(raw: Any) -> dict[str, Any]:
    """Decode an optional SQLite JSON object, discarding malformed values."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, RecursionError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def read_judgments(conn: sqlite3.Connection, tables: set[str]) -> tuple[dict, dict]:
    """Index verdicts by profile and identity, and by most recent judgment per identity."""
    by_profile, latest = {}, {}
    if "scout_verdicts" in tables:
        sql = ("SELECT identity_key, profile_slug, verdict, priority, row_json FROM scout_verdicts"
               " ORDER BY recorded_at DESC, profile_slug")
        for key, profile, verdict, priority, raw in conn.execute(sql):
            judgment = {**json_object(raw), "verdict": verdict, "priority": priority}
            by_profile.setdefault((key, profile), judgment)
            latest.setdefault(key, judgment)
    return by_profile, latest


def read_profile_feedback(conn: sqlite3.Connection, tables: set[str]) -> dict[str, dict]:
    """Index sanitized feedback by posting and profile."""
    result = defaultdict(dict)
    if "profile_job_feedback" in tables:
        sql = ("SELECT identity_key, profile_slug, relevance, reason_code, note, labeled_at"
               " FROM profile_job_feedback ORDER BY profile_slug")
        for key, profile, relevance, reason, note, labeled_at in conn.execute(sql):
            result[key][profile] = {
                "relevance": sanitize.normalize_token(relevance, RELEVANCE, default=""),
                "reason_code": sanitize.clean_text(reason),
                "note": sanitize.clean_multiline_text(note),
                "labeled_at": sanitize.clean_text(labeled_at),
            }
    return result


def enrich_job(job: dict[str, Any], judgment: dict, feedback: dict, profile: str) -> None:
    """Add toolkit display data, preferring the job's profile feedback."""
    remote = sanitize.normalize_token(judgment.get("remote_policy"), get_args(RemotePolicy), default="unknown")
    dates = {key: iso_vacancy_date(job[key]) or iso_vacancy_date(judgment.get(key))
             for key in ("published_at", "updated_at", "unverified_date")}
    job.update(
        dates,
        priority=view_model.normalize_priority(judgment.get("priority")),
        verdict=sanitize.normalize_token(judgment.get("verdict"), ("keep", "unsure", "discard"), default=""),
        remote_policy=remote,
        remote_label=view_model.remote_label(remote),
        eligibility=sanitize.normalize_token(judgment.get("eligibility"), get_args(Eligibility), default=""),
        eligibility_evidence=sanitize.clean_text(judgment.get("eligibility_evidence"))[:300],
        fit_signals=view_model.split_fit_signals(judgment.get("fit_signals")),
        salary=sanitize.clean_text(judgment.get("salary"))[:120],
        found_by=sanitize.clean_text(judgment.get("found_by"))[:60],
        profile_feedback={sanitize.clean_text(key): value for key, value in feedback.items()},
        best_date=view_model.best_vacancy_date({**job, **dates, "freshness": judgment.get("freshness")}),
        monogram=view_model.company_monogram(job["company"]),
    )
    if scoped := feedback.get(profile):
        job.update(feedback_relevance=scoped["relevance"], feedback_reason=scoped["reason_code"],
                   feedback_note=scoped["note"], feedback_at=scoped["labeled_at"])


def read_watchlist(conn: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
    """Read explicit board settings and the effective history-backed watchlist."""
    if "watchlist" not in tables:
        return []
    items = [dict(row) for row in conn.execute(
        "SELECT profile_slug, seed_key, company, state, added_by, added_at FROM watchlist"
        " ORDER BY profile_slug, seed_key"
    )]
    if "seed_yield_runs" in tables:
        profiles = {row[0] for row in conn.execute("SELECT DISTINCT profile_slug FROM seed_yield_runs")}
        for profile in sorted(profiles):
            items.extend({"profile_slug": profile, "seed_key": board["seed_key"], "company": board["company"],
                          "state": "watch", "added_by": "history", "added_at": ""}
                         for board in watched(conn, profile) if board["source"] == "history")
    return [{field: sanitize.clean_text(value) for field, value in row.items()} for row in items]


def read_watch_pending(conn: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
    """Project pending candidate JSON into a small, safe set of display fields."""
    if "watch_pending" not in tables:
        return []
    items = []
    for profile, key, raw, found_at in conn.execute(
        "SELECT profile_slug, identity_key, candidate_json, found_at FROM watch_pending"
        " WHERE state = 'pending' ORDER BY found_at DESC, profile_slug, identity_key"
    ):
        candidate = json_object(raw)
        items.append({
            "profile_slug": sanitize.clean_text(profile),
            "identity_key": sanitize.clean_text(key),
            "title": sanitize.clean_text(candidate.get("ats_title")),
            "company": sanitize.clean_text(candidate.get("ats_company")),
            "location": sanitize.clean_text(candidate.get("ats_location")),
            "url": sanitize.safe_http_url(candidate.get("normalized_url") or candidate.get("url")),
            "found_at": sanitize.clean_text(found_at),
            "source_key": sanitize.clean_text(candidate.get("source_key")),
        })
    return items
