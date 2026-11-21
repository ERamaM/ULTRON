"""Read-only SQLite ledger snapshot builder for the local web viewer export."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ultron import __version__
from ultron.exports import sanitize
from ultron.infra import home

_BANDS = ("strong", "good", "borderline", "ineligible")
_STATES = ("unreviewed", "shortlisted", "applied", "rejected", "archived")
_RELEVANCE = ("relevant", "not_relevant", "uncertain")
_DIAGNOSTIC_LIMIT = 200


def default_db_path() -> Path:
    """Return the default SQLite ledger file location under ``$ULTRON_HOME``."""
    return home.ultron_home() / "ultron.sqlite3"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {str(row[0]) for row in rows}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    # ``table`` is always a static identifier validated against allowed ledger table names.
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row["name"]) for row in rows}


def _parse_json_dict(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(str(raw))
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return _sanitize_value(parsed, depth=0)


def _sanitize_value(value: Any, *, depth: int) -> Any:
    if depth >= 4:
        return sanitize.clean_text(value)
    if isinstance(value, dict):
        return {sanitize.clean_text(k): _sanitize_value(v, depth=depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item, depth=depth + 1) for item in value[:50]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return sanitize.clean_text(value)


def _empty_counts(keys: tuple[str, ...], *, extra: str | None = None) -> dict[str, int]:
    counts = {key: 0 for key in keys}
    if extra:
        counts[extra] = 0
    return counts


def _read_occurrences(conn: sqlite3.Connection, tables: set[str]) -> tuple[dict[str, list[dict[str, Any]]], Counter]:
    by_job: dict[str, list[dict[str, Any]]] = {}
    new_per_run: Counter[str] = Counter()
    if "job_occurrences" not in tables:
        return by_job, new_per_run
    cols = _columns(conn, "job_occurrences")
    select_is_new = "is_new" if "is_new" in cols else "0 AS is_new"
    select_seen_at = "seen_at" if "seen_at" in cols else "NULL AS seen_at"
    sql = f"SELECT identity_key, run_id, {select_is_new}, {select_seen_at} FROM job_occurrences ORDER BY rowid DESC"
    for row in conn.execute(sql):
        key = sanitize.clean_text(row["identity_key"])
        run_id = sanitize.clean_text(row["run_id"])
        is_new = bool(row["is_new"])
        if not key or not run_id:
            continue
        by_job.setdefault(key, []).append(
            {
                "run_id": run_id,
                "is_new": is_new,
                "seen_at": sanitize.clean_text(row["seen_at"]) or None,
            }
        )
        if is_new:
            new_per_run[run_id] += 1
    return by_job, new_per_run


def _read_jobs(
    conn: sqlite3.Connection,
    tables: set[str],
    occurrences_by_job: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if "jobs" not in tables:
        return []
    cols = _columns(conn, "jobs")
    optional = (
        ("location", "''"),
        ("source_type", "''"),
        ("source_key", "''"),
        ("first_seen_run_id", "NULL"),
        ("last_seen_run_id", "NULL"),
        ("review_changed_at", "NULL"),
        ("notes", "''"),
        ("match_score", "NULL"),
        ("match_band", "NULL"),
        ("profile_slug", "''"),
    )
    fields = ["j.identity_key", "j.canonical_url", "j.title", "j.company", "j.first_seen", "j.last_seen", "j.times_seen", "j.review_state"]
    for name, fallback in optional:
        fields.append(f"j.{name}" if name in cols else f"{fallback} AS {name}")

    has_feedback = "job_feedback" in tables
    if has_feedback:
        fields.extend(["f.relevance AS feedback_relevance", "f.reason_code AS feedback_reason", "f.note AS feedback_note", "f.labeled_at AS feedback_at"])
        join_sql = " LEFT JOIN job_feedback f ON f.identity_key = j.identity_key"
    else:
        fields.extend(["NULL AS feedback_relevance", "'' AS feedback_reason", "'' AS feedback_note", "NULL AS feedback_at"])
        join_sql = ""

    sql = f"SELECT {', '.join(fields)} FROM jobs j{join_sql} ORDER BY j.last_seen DESC, j.identity_key"
    items: list[dict[str, Any]] = []
    for row in conn.execute(sql):
        key = sanitize.clean_text(row["identity_key"])
        occ = occurrences_by_job.get(key, [])
        items.append(
            {
                "identity_key": key,
                "canonical_url": sanitize.safe_http_url(row["canonical_url"]),
                "title": sanitize.clean_text(row["title"]) or "Untitled posting",
                "company": sanitize.clean_text(row["company"]),
                "location": sanitize.clean_text(row["location"]),
                "source_type": sanitize.clean_text(row["source_type"]),
                "source_key": sanitize.clean_text(row["source_key"]),
                "first_seen": sanitize.clean_text(row["first_seen"]),
                "last_seen": sanitize.clean_text(row["last_seen"]),
                "times_seen": max(0, int(row["times_seen"] or 0)),
                "review_state": sanitize.normalize_token(row["review_state"], _STATES, default="unreviewed"),
                "review_changed_at": sanitize.clean_text(row["review_changed_at"]) or None,
                "first_seen_run_id": sanitize.clean_text(row["first_seen_run_id"]) or None,
                "last_seen_run_id": sanitize.clean_text(row["last_seen_run_id"]) or None,
                "notes": sanitize.clean_multiline_text(row["notes"]),
                "match_score": sanitize.clamp_score(row["match_score"]),
                "match_band": sanitize.normalize_token(row["match_band"], _BANDS, default="unscored"),
                "profile_slug": sanitize.clean_text(row["profile_slug"]),
                "feedback_relevance": sanitize.normalize_token(row["feedback_relevance"], _RELEVANCE, default="")
                if row["feedback_relevance"]
                else None,
                "feedback_reason": sanitize.clean_text(row["feedback_reason"]),
                "feedback_note": sanitize.clean_multiline_text(row["feedback_note"]),
                "feedback_at": sanitize.clean_text(row["feedback_at"]) or None,
                "run_ids": [entry["run_id"] for entry in occ],
                "occurrences": occ,
            }
        )
    return items


def _read_runs(conn: sqlite3.Connection, tables: set[str], new_per_run: Counter) -> list[dict[str, Any]]:
    if "runs" not in tables:
        return []
    sql = (
        "SELECT run_id, profile_slug, preset, created_at, finished_at, roles_kept, discard_counts_json"
        " FROM runs ORDER BY COALESCE(created_at, '') DESC, run_id DESC"
    )
    items: list[dict[str, Any]] = []
    for row in conn.execute(sql):
        run_id = sanitize.clean_text(row["run_id"])
        raw_discards = _parse_json_dict(row["discard_counts_json"])
        discard_counts = {k: int(v) for k, v in raw_discards.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        items.append(
            {
                "run_id": run_id,
                "profile_slug": sanitize.clean_text(row["profile_slug"]),
                "preset": sanitize.clean_text(row["preset"]),
                "created_at": sanitize.clean_text(row["created_at"]) or None,
                "finished_at": sanitize.clean_text(row["finished_at"]) or None,
                "roles_kept": max(0, int(row["roles_kept"] or 0)),
                "new_roles_count": new_per_run.get(run_id, 0),
                "discard_counts": discard_counts,
            }
        )
    return items


def _read_seed_yields(conn: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
    if "seed_yield_runs" not in tables:
        return []
    sql = (
        "SELECT profile_slug, seed_key, COUNT(*) AS attempts,"
        " SUM(CASE WHEN kept > 0 THEN 1 ELSE 0 END) AS runs_with_keeps,"
        " COALESCE(SUM(kept), 0) AS total_kept"
        " FROM seed_yield_runs GROUP BY profile_slug, seed_key"
        " ORDER BY total_kept DESC, runs_with_keeps DESC, attempts DESC, seed_key"
    )
    items: list[dict[str, Any]] = []
    for row in conn.execute(sql):
        attempts = max(0, int(row["attempts"] or 0))
        with_keeps = max(0, int(row["runs_with_keeps"] or 0))
        pct = round((with_keeps / attempts) * 100, 1) if attempts else 0.0
        items.append(
            {
                "profile_slug": sanitize.clean_text(row["profile_slug"]),
                "seed_key": sanitize.clean_text(row["seed_key"]),
                "attempts": attempts,
                "runs_with_keeps": with_keeps,
                "total_kept": max(0, int(row["total_kept"] or 0)),
                "yield_pct": pct,
            }
        )
    return items


def _read_saved_searches(conn: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
    if "saved_searches" not in tables:
        return []
    sql = "SELECT name, profile_slug, preset, prompt, overrides_json, created_at, updated_at FROM saved_searches ORDER BY name"
    return [
        {
            "name": sanitize.clean_text(row["name"]),
            "profile_slug": sanitize.clean_text(row["profile_slug"]),
            "preset": sanitize.clean_text(row["preset"]),
            "prompt": sanitize.clean_text(row["prompt"]),
            "overrides": _parse_json_dict(row["overrides_json"]),
            "created_at": sanitize.clean_text(row["created_at"]) or None,
            "updated_at": sanitize.clean_text(row["updated_at"]) or None,
        }
        for row in conn.execute(sql)
    ]


def _read_diagnostics(conn: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
    if "diagnostics" not in tables:
        return []
    sql = (
        "SELECT id, submitted_url, identity_key, first_failed_stage, reason_code, trace_json, created_at"
        " FROM diagnostics ORDER BY id DESC LIMIT ?"
    )
    return [
        {
            "id": int(row["id"]),
            "submitted_url": sanitize.safe_http_url(row["submitted_url"]),
            "identity_key": sanitize.clean_text(row["identity_key"]),
            "first_failed_stage": sanitize.clean_text(row["first_failed_stage"]),
            "reason_code": sanitize.clean_text(row["reason_code"]),
            "trace": _parse_json_dict(row["trace_json"]),
            "created_at": sanitize.clean_text(row["created_at"]) or None,
        }
        for row in conn.execute(sql, (_DIAGNOSTIC_LIMIT,))
    ]


def _build_summary(
    jobs: list[dict[str, Any]],
    runs_list: list[dict[str, Any]],
    seed_yields: list[dict[str, Any]],
    saved_searches: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> dict[str, Any]:
    band_counts = _empty_counts(_BANDS, extra="unscored")
    state_counts = _empty_counts(_STATES)
    feedback_counts = _empty_counts(_RELEVANCE)
    companies: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    profiles: set[str] = {r["profile_slug"] for r in runs_list if r["profile_slug"]}
    scores: list[int] = []
    new_jobs = 0

    for job in jobs:
        band_counts[job["match_band"]] = band_counts.get(job["match_band"], 0) + 1
        state_counts[job["review_state"]] = state_counts.get(job["review_state"], 0) + 1
        if job["feedback_relevance"]:
            feedback_counts[job["feedback_relevance"]] = feedback_counts.get(job["feedback_relevance"], 0) + 1
        if job["match_score"] is not None:
            scores.append(job["match_score"])
        if job["times_seen"] == 1:
            new_jobs += 1
        if job["company"]:
            companies[job["company"]] += 1
        if job["source_type"]:
            sources[job["source_type"]] += 1
        if job["profile_slug"]:
            profiles.add(job["profile_slug"])

    return {
        "total_jobs": len(jobs),
        "new_jobs": new_jobs,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "band_counts": band_counts,
        "state_counts": state_counts,
        "feedback_counts": feedback_counts,
        "total_runs": len(runs_list),
        "seed_count": len(seed_yields),
        "saved_search_count": len(saved_searches),
        "diagnostic_count": len(diagnostics),
        "profile_count": len(profiles),
        "profiles": sorted(profiles),
        "company_count": len(companies),
        "top_companies": [{"name": name, "count": count} for name, count in companies.most_common(10)],
        "top_sources": [{"name": name, "count": count} for name, count in sources.most_common(10)],
    }


def build_ledger_snapshot(db_path: Path | None = None) -> dict[str, Any]:
    """Read ``$ULTRON_HOME/ultron.sqlite3`` strictly read-only into a sanitized snapshot."""
    target = Path(db_path) if db_path is not None else default_db_path()
    now_iso = datetime.now(UTC).isoformat()
    if not target.is_file():
        return {
            "meta": {
                "db_name": target.name,
                "db_exists": False,
                "schema_version": 0,
                "ultron_version": __version__,
                "generated_at": now_iso,
                "db_updated_at": None,
            },
            "summary": _build_summary([], [], [], [], []),
            "jobs": [],
            "runs": [],
            "seed_yields": [],
            "saved_searches": [],
            "diagnostics": [],
        }

    db_updated_at = datetime.fromtimestamp(target.stat().st_mtime, tz=UTC).isoformat()
    with closing(_connect_readonly(target)) as conn:
        tables = _tables(conn)
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        occurrences_by_job, new_per_run = _read_occurrences(conn, tables)
        jobs = _read_jobs(conn, tables, occurrences_by_job)
        runs_list = _read_runs(conn, tables, new_per_run)
        seed_yields = _read_seed_yields(conn, tables)
        saved_searches = _read_saved_searches(conn, tables)
        diagnostics = _read_diagnostics(conn, tables)

    return {
        "meta": {
            "db_name": target.name,
            "db_exists": True,
            "schema_version": schema_version,
            "ultron_version": __version__,
            "generated_at": now_iso,
            "db_updated_at": db_updated_at,
        },
        "summary": _build_summary(jobs, runs_list, seed_yields, saved_searches, diagnostics),
        "jobs": jobs,
        "runs": runs_list,
        "seed_yields": seed_yields,
        "saved_searches": saved_searches,
        "diagnostics": diagnostics,
    }
