"""Review workflow over the job ledger: states, notes, feedback and "what is new"."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime

from ultron.core.job_identity import canonical_job_identity
from ultron.storage import ledger

STATES = ("unreviewed", "shortlisted", "applied", "rejected", "archived")
RELEVANCE = ("relevant", "not_relevant", "uncertain")
REASON_CODES = ("", "wrong_role", "seniority", "location", "remote", "eligibility", "closed", "duplicate", "other")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def resolve_key(key_or_url: str, conn=None) -> str:
    """Accept an identity key or a URL (canonicalised).

    A job may have been recorded with source hints (``source_key="ashby:acme"``) that give it a different
    identity than the bare URL. With ``conn``, a URL whose bare identity is not in the ledger resolves to the
    stored job with the same canonical URL.
    """
    text = key_or_url.strip()
    if not text.lower().startswith(("http://", "https://")):
        return text
    ident = canonical_job_identity(text)
    if conn is not None and conn.execute(
        "SELECT 1 FROM jobs WHERE identity_key = ?", (ident.identity_key,)
    ).fetchone() is None:
        row = conn.execute(
            "SELECT identity_key FROM jobs WHERE canonical_url = ? ORDER BY last_seen DESC LIMIT 1",
            (ident.canonical_url,),
        ).fetchone()
        if row is not None:
            return row[0]
    return ident.identity_key


def _require(conn, key: str) -> None:
    if conn.execute("SELECT 1 FROM jobs WHERE identity_key = ?", (key,)).fetchone() is None:
        raise LookupError(f"job not found: {key}")


def _rows(conn, sql: str, params: tuple = ()) -> list[dict]:
    cur = conn.execute(sql, params)
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r, strict=True)) for r in cur.fetchall()]


_SELECT = (
    "SELECT j.*, f.relevance AS feedback_relevance, f.reason_code AS feedback_reason,"
    " CASE WHEN f.relevance IS NOT NULL THEN 'unscoped' END AS feedback_scope"
    " FROM jobs j LEFT JOIN job_feedback f ON f.identity_key = j.identity_key"
)


def _run_created_at(conn, run_id: str) -> str:
    row = conn.execute("SELECT created_at FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if row is None or row[0] is None:
        raise LookupError(f"run not found in ledger: {run_id}")
    return row[0]


def _resolve_scope(conn, profile: str | None, saved_search: str | None, baseline: str | None):
    if not saved_search:
        return "profile_slug", profile, {"kind": "profile", "profile": profile}
    row = conn.execute(
        "SELECT profile_slug, created_at FROM saved_searches WHERE name = ?", (saved_search,),
    ).fetchone()
    if row is None:
        raise LookupError(f"saved search not found: {saved_search}")
    slug, created = row
    search_id = ledger.saved_search_identity(saved_search, created)
    scope = {"kind": "saved_search", "saved_search": saved_search, "profile": slug}
    if baseline:
        provenance = conn.execute(
            "SELECT profile_slug, saved_search_id FROM run_scopes WHERE run_id = ?", (baseline,),
        ).fetchone()
        if provenance == (slug, None):
            return "profile_slug", slug, {"kind": "profile", "profile": slug, "requested_search": saved_search}
    return "saved_search_id", search_id, scope


def _scoped_rows(
    conn, *, profile: str | None, saved_search: str | None, since_run: str | None,
    new_only: bool, state: str | None, limit: int | None,
) -> tuple[list[dict], dict]:
    column, value, scope = _resolve_scope(conn, profile, saved_search, since_run)
    field = "first_run" if new_only else "last_run"
    condition = ""
    params = [value, scope["profile"]]
    if since_run:
        baseline = conn.execute(
            f"SELECT created_at FROM run_scopes WHERE run_id = ? AND {column} = ?", (since_run, value),
        ).fetchone()
        if baseline is None or baseline[0] is None:
            raise ValueError("baseline run is unavailable in the selected search scope")
        comparator = ">" if new_only else ">="
        condition += f" AND {field} {comparator} ?"
        params.append(f"{baseline[0]}|{since_run}")
    elif new_only:
        condition += " AND first_run = (SELECT MAX(run_order) FROM scoped_runs)"
    if state:
        condition += " AND j.review_state = ?"
        params.append(state)
    sql = (
        "WITH scoped_runs AS (SELECT run_id, created_at || '|' || run_id AS run_order"
        f" FROM run_scopes WHERE {column} = ? AND created_at IS NOT NULL),"
        " sightings AS (SELECT o.identity_key, MIN(r.run_order) AS first_run, MAX(r.run_order) AS last_run"
        " FROM scoped_runs r JOIN job_occurrences o USING (run_id) GROUP BY o.identity_key)"
        " SELECT j.*, scoped.details_json AS scoped_details, s.first_run AS scoped_first_seen,"
        " s.last_run AS scoped_last_seen,"
        " COALESCE(p.relevance, f.relevance) AS feedback_relevance,"
        " COALESCE(p.reason_code, f.reason_code) AS feedback_reason,"
        " CASE WHEN p.relevance IS NOT NULL THEN 'profile' WHEN f.relevance IS NOT NULL THEN 'unscoped' END"
        " AS feedback_scope, f.relevance AS unscoped_feedback_relevance"
        " FROM sightings s JOIN jobs j USING (identity_key)"
        " JOIN scoped_runs latest ON latest.run_order = s.last_run"
        " JOIN job_occurrences scoped ON scoped.run_id = latest.run_id AND scoped.identity_key = j.identity_key"
        " LEFT JOIN profile_job_feedback p ON p.identity_key = j.identity_key AND p.profile_slug = ?"
        " LEFT JOIN job_feedback f ON f.identity_key = j.identity_key WHERE 1 = 1"
        f"{condition} ORDER BY s.last_run DESC, j.identity_key"
    )
    if limit is not None:
        sql += " LIMIT ?"
        params.append(max(0, limit))
    found = _rows(conn, sql, tuple(params))
    for row in found:
        detail = ledger.occurrence_details(row.pop("scoped_details"))
        for field in ("title", "company", "location", "canonical_url", "source_type", "source_key"):
            if field in detail:
                row[field] = detail[field]
        row["match_score"] = detail.get("match_score")
        row["match_band"] = detail.get("match_band")
        row["profile_slug"] = detail.get("profile_slug", scope["profile"])
        row["first_seen"], row["first_seen_run_id"] = row.pop("scoped_first_seen").split("|", 1)
        row["last_seen"], row["last_seen_run_id"] = row.pop("scoped_last_seen").split("|", 1)
    return found, scope


def list_scoped_jobs(
    *, profile: str | None = None, saved_search: str | None = None, since_run: str | None = None,
    new_only: bool = False, state: str | None = None, limit: int | None = None,
) -> tuple[list[dict], dict]:
    """Return vacancies and the actual occurrence scope used for the comparison."""
    if state is not None and state not in STATES:
        raise ValueError(f"invalid state {state!r}")
    if bool(profile) == bool(saved_search):
        raise ValueError("select one profile or saved search")
    with closing(ledger.connect()) as conn:
        return _scoped_rows(
            conn, profile=profile, saved_search=saved_search, since_run=since_run, new_only=new_only,
            state=state, limit=limit,
        )


def list_jobs(
    *, state: str | None = None, profile: str | None = None, since_run: str | None = None,
    new_only: bool = False, limit: int = 50,
) -> list[dict]:
    """Jobs, most recently seen first.

    ``since_run``: last seen in a run created at/after it; ``new_only``: seen in exactly one run.
    """
    if profile is not None:
        jobs, _scope = list_scoped_jobs(
            profile=profile, state=state, since_run=since_run, new_only=new_only, limit=limit,
        )
        return jobs
    if state is not None and state not in STATES:
        raise ValueError(f"invalid state {state!r}; expected one of {', '.join(STATES)}")
    where: list[str] = []
    params: list = []
    with closing(ledger.connect()) as conn:
        if state is not None:
            where.append("j.review_state = ?")
            params.append(state)

        if since_run is not None:
            where.append("(SELECT r.created_at FROM runs r WHERE r.run_id = j.last_seen_run_id) >= ?")
            params.append(_run_created_at(conn, since_run))
        if new_only:
            where.append("j.times_seen = 1")
        sql = _SELECT + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY j.last_seen DESC, j.identity_key LIMIT ?"
        return _rows(conn, sql, (*params, max(0, limit)))


def set_state(identity_key: str, state: str) -> str:
    """Set the review state; returns the resolved identity key."""
    if state not in STATES:
        raise ValueError(f"invalid state {state!r}; expected one of {', '.join(STATES)}")
    with closing(ledger.connect()) as conn, conn:
        key = resolve_key(identity_key, conn)
        _require(conn, key)
        conn.execute(
            "UPDATE jobs SET review_state = ?, review_changed_at = ? WHERE identity_key = ?", (state, _now(), key)
        )
    return key


def add_note(identity_key: str, text: str) -> str:
    """Append a timestamped note; returns the resolved identity key."""
    line = f"[{_now()}] {text}"
    with closing(ledger.connect()) as conn, conn:
        key = resolve_key(identity_key, conn)
        _require(conn, key)
        conn.execute(
            "UPDATE jobs SET notes = CASE WHEN notes = '' THEN ? ELSE notes || char(10) || ? END"
            " WHERE identity_key = ?",
            (line, line, key),
        )
    return key


def set_feedback(
    identity_key: str, relevance: str, reason_code: str = "", note: str = "", *, profile: str | None = None,
) -> str:
    """Record relevance feedback and optional reason code/note on a job."""
    if relevance not in RELEVANCE:
        raise ValueError(f"invalid relevance {relevance!r}; expected one of {', '.join(RELEVANCE)}")
    if reason_code not in REASON_CODES:
        valid = ", ".join(c for c in REASON_CODES if c)
        raise ValueError(f"invalid reason_code {reason_code!r}; expected one of {valid}")
    with closing(ledger.connect()) as conn, conn:
        key = resolve_key(identity_key, conn)
        _require(conn, key)
        if profile:
            conn.execute(
                "INSERT INTO profile_job_feedback (identity_key, profile_slug, relevance, reason_code, note, labeled_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(identity_key, profile_slug) DO UPDATE SET"
                " relevance = excluded.relevance, reason_code = excluded.reason_code,"
                " note = excluded.note, labeled_at = excluded.labeled_at",
                (key, profile, relevance, reason_code, note, _now()),
            )
            return key
        conn.execute(
            "INSERT INTO job_feedback (identity_key, relevance, reason_code, note, labeled_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(identity_key) DO UPDATE SET relevance = excluded.relevance,"
            " reason_code = excluded.reason_code, note = excluded.note, labeled_at = excluded.labeled_at",
            (key, relevance, reason_code, note, _now()),
        )
    return key


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
