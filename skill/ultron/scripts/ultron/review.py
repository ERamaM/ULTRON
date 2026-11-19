"""Review workflow over the job ledger: states, notes, feedback and "what is new"."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime

from ultron import ledger
from ultron.core.job_identity import canonical_job_identity

STATES = ("unreviewed", "shortlisted", "applied", "rejected", "archived")
RELEVANCE = ("relevant", "not_relevant", "uncertain")
REASON_CODES = ("", "wrong_role", "seniority", "location", "remote", "eligibility", "closed", "duplicate", "other")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def resolve_key(key_or_url: str) -> str:
    """Accept an identity key or a URL (canonicalised)."""
    text = key_or_url.strip()
    if text.lower().startswith(("http://", "https://")):
        return canonical_job_identity(text).identity_key
    return text


def _require(conn, key: str) -> None:
    if conn.execute("SELECT 1 FROM jobs WHERE identity_key = ?", (key,)).fetchone() is None:
        raise LookupError(f"job not found: {key}")


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


_SELECT = (
    "SELECT j.*, f.relevance AS feedback_relevance, f.reason_code AS feedback_reason"
    " FROM jobs j LEFT JOIN job_feedback f ON f.identity_key = j.identity_key"
)


def _run_created_at(conn, run_id: str) -> str:
    row = conn.execute("SELECT created_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None or row[0] is None:
        raise LookupError(f"run not found in ledger: {run_id}")
    return row[0]


def list_jobs(
    *, state: str | None = None, profile: str | None = None, since_run: str | None = None,
    new_only: bool = False, limit: int = 50,
) -> list[dict]:
    """Jobs, most recently seen first.

    ``since_run``: last seen in a run created at/after it; ``new_only``: seen in exactly one run.
    """
    if state is not None and state not in STATES:
        raise ValueError(f"invalid state {state!r}; expected one of {', '.join(STATES)}")
    where: list[str] = []
    params: list = []
    with closing(ledger.connect()) as conn:
        if state is not None:
            where.append("j.review_state = ?")
            params.append(state)
        if profile is not None:
            where.append("j.profile_slug = ?")
            params.append(profile)
        if since_run is not None:
            where.append("(SELECT r.created_at FROM runs r WHERE r.run_id = j.last_seen_run_id) >= ?")
            params.append(_run_created_at(conn, since_run))
        if new_only:
            where.append("j.times_seen = 1")
        sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY j.last_seen DESC, j.identity_key LIMIT ?"
        return _rows(conn, sql, (*params, max(0, limit)))


def set_state(identity_key: str, state: str) -> None:
    if state not in STATES:
        raise ValueError(f"invalid state {state!r}; expected one of {', '.join(STATES)}")
    key = resolve_key(identity_key)
    with closing(ledger.connect()) as conn, conn:
        _require(conn, key)
        conn.execute(
            "UPDATE jobs SET review_state = ?, review_changed_at = ? WHERE identity_key = ?", (state, _now(), key)
        )


def add_note(identity_key: str, text: str) -> None:
    key = resolve_key(identity_key)
    line = f"[{_now()}] {text}"
    with closing(ledger.connect()) as conn, conn:
        _require(conn, key)
        conn.execute(
            "UPDATE jobs SET notes = CASE WHEN notes = '' THEN ? ELSE notes || char(10) || ? END"
            " WHERE identity_key = ?",
            (line, line, key),
        )


def set_feedback(identity_key: str, relevance: str, reason_code: str = "", note: str = "") -> None:
    if relevance not in RELEVANCE:
        raise ValueError(f"invalid relevance {relevance!r}; expected one of {', '.join(RELEVANCE)}")
    if reason_code not in REASON_CODES:
        valid = ", ".join(c for c in REASON_CODES if c)
        raise ValueError(f"invalid reason_code {reason_code!r}; expected one of {valid}")
    key = resolve_key(identity_key)
    with closing(ledger.connect()) as conn, conn:
        _require(conn, key)
        conn.execute(
            "INSERT INTO job_feedback (identity_key, relevance, reason_code, note, labeled_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(identity_key) DO UPDATE SET relevance = excluded.relevance,"
            " reason_code = excluded.reason_code, note = excluded.note, labeled_at = excluded.labeled_at",
            (key, relevance, reason_code, note, _now()),
        )


def new_since(run_id: str) -> list[dict]:
    """Jobs first seen in a run created after ``run_id``."""
    with closing(ledger.connect()) as conn:
        created = _run_created_at(conn, run_id)
        return _rows(
            conn,
            _SELECT + " WHERE (SELECT r.created_at FROM runs r WHERE r.run_id = j.first_seen_run_id) > ?"
            " ORDER BY j.first_seen DESC, j.identity_key",
            (created,),
        )
