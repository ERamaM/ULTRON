"""The watchlist of boards per profile and the postings ``ultron watch`` found for the next scout run.

A board is watched when the user added it, or when it produced a kept posting for the profile or for a
profile of the same role family. A board the user removed stays blocked, so it is never added back. A board
this profile queried five times without a keep is left out.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime

DEMOTE_ATTEMPTS = 5


def _now() -> str:
    return datetime.now(UTC).isoformat()


def add(conn: sqlite3.Connection, profile_slug: str, seed_key: str, company: str = "", *, added_by: str = "user") -> None:
    """Watch ``seed_key`` for ``profile_slug`` (unblocks a board the user removed earlier)."""
    with conn:
        conn.execute(
            "INSERT INTO watchlist (profile_slug, seed_key, company, state, added_by, added_at)"
            " VALUES (?, ?, ?, 'watch', ?, ?) ON CONFLICT(profile_slug, seed_key) DO UPDATE SET"
            " state = 'watch', company = CASE WHEN excluded.company != '' THEN excluded.company ELSE company END",
            (profile_slug, seed_key, company, added_by, _now()),
        )


def remove(conn: sqlite3.Connection, profile_slug: str, seed_key: str) -> None:
    """Stop watching ``seed_key`` for ``profile_slug`` and never add it back automatically."""
    with conn:
        conn.execute(
            "INSERT INTO watchlist (profile_slug, seed_key, state, added_by, added_at) VALUES (?, ?, 'blocked', 'user', ?)"
            " ON CONFLICT(profile_slug, seed_key) DO UPDATE SET state = 'blocked'",
            (profile_slug, seed_key, _now()),
        )


def _yields(conn: sqlite3.Connection, slugs: list[str]) -> dict[str, tuple[int, int]]:
    marks = ", ".join("?" for _ in slugs)
    return {
        key: (attempts, keeps or 0)
        for key, attempts, keeps in conn.execute(
            f"SELECT seed_key, COUNT(*), SUM(kept) FROM seed_yield_runs WHERE profile_slug IN ({marks})"
            " GROUP BY seed_key", slugs,
        )
    }


def watched(conn: sqlite3.Connection, profile_slug: str, siblings: Iterable[str] = ()) -> list[dict]:
    """The boards to watch for ``profile_slug``, user-added first, then by kept postings.

    ``siblings`` are the other profiles of the same role family; their keeps count too. Each item has
    ``seed_key``, ``company``, ``source`` (``user`` or ``history``) and ``keeps``.
    """
    entries = {key: (company, state) for key, company, state in conn.execute(
        "SELECT seed_key, company, state FROM watchlist WHERE profile_slug = ?", (profile_slug,))}
    own = _yields(conn, [profile_slug])
    family = _yields(conn, [profile_slug, *[s for s in siblings if s != profile_slug]])
    out = [{"seed_key": key, "company": company, "source": "user", "keeps": family.get(key, (0, 0))[1]}
           for key, (company, state) in sorted(entries.items()) if state == "watch"]
    chosen = {item["seed_key"] for item in out}
    history = sorted(
        ((key, keeps) for key, (_attempts, keeps) in family.items() if keeps > 0),
        key=lambda item: (-item[1], item[0]),
    )
    for key, keeps in history:
        attempts, own_keeps = own.get(key, (0, 0))
        demoted = attempts >= DEMOTE_ATTEMPTS and own_keeps == 0
        if key in chosen or entries.get(key, ("", ""))[1] == "blocked" or demoted:
            continue
        out.append({"seed_key": key, "company": "", "source": "history", "keeps": keeps})
    return out


def entries(conn: sqlite3.Connection, profile_slug: str) -> list[dict]:
    """The watchlist rows the user can see: every added or blocked board of the profile."""
    return [{"seed_key": key, "company": company, "state": state, "added_by": added_by}
            for key, company, state, added_by in conn.execute(
                "SELECT seed_key, company, state, added_by FROM watchlist WHERE profile_slug = ? ORDER BY seed_key",
                (profile_slug,))]


def add_pending(conn: sqlite3.Connection, profile_slug: str, items: Iterable[tuple[str, dict]]) -> list[str]:
    """Record postings found by a watch; return the identity keys that were not pending already."""
    added: list[str] = []
    with conn:
        for key, candidate in items:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO watch_pending (profile_slug, identity_key, candidate_json, found_at)"
                " VALUES (?, ?, ?, ?)",
                (profile_slug, key, json.dumps(candidate, ensure_ascii=False), _now()),
            )
            if cursor.rowcount:
                added.append(key)
    return added


def pending(conn: sqlite3.Connection, profile_slug: str) -> list[dict]:
    """Watched postings still waiting for an agent, oldest first, as candidate mappings."""
    return [json.loads(text) for (text,) in conn.execute(
        "SELECT candidate_json FROM watch_pending WHERE profile_slug = ? AND state = 'pending'"
        " ORDER BY found_at, identity_key", (profile_slug,))]


def known_pending(conn: sqlite3.Connection, profile_slug: str) -> set[str]:
    """Identity keys a watch already recorded for the profile, queued or not."""
    return {key for (key,) in conn.execute(
        "SELECT identity_key FROM watch_pending WHERE profile_slug = ?", (profile_slug,))}


def return_to_pending(conn: sqlite3.Connection, profile_slug: str, keys: Iterable[str]) -> int:
    """Put watched postings a scout run did not evaluate back in the pending list for the next run."""
    keys = list(keys)
    with conn:
        conn.executemany(
            "UPDATE watch_pending SET state = 'pending', run_id = NULL WHERE profile_slug = ? AND identity_key = ?",
            [(profile_slug, key) for key in keys],
        )
    return len(keys)


def mark_queued(conn: sqlite3.Connection, profile_slug: str, keys: Iterable[str], run_id: str) -> int:
    """Hand pending postings to scout run ``run_id``."""
    keys = list(keys)
    with conn:
        conn.executemany(
            "UPDATE watch_pending SET state = 'queued', run_id = ? WHERE profile_slug = ? AND identity_key = ?",
            [(run_id, profile_slug, key) for key in keys],
        )
    return len(keys)
