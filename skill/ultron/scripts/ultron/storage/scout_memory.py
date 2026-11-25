"""Agent memory: what a profile already saw, rejected or applied to, and the agent's last verdicts.

``inspect`` and ``search`` read it to flag every posting (``new``, ``seen``, ``rejected``, ``applied``), and
``shortlist save`` stores the agent's verdict of every saved posting per profile, with its exported row.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

Flag = Literal["new", "seen", "rejected", "applied"]
USER_REJECTED_STATES = ("rejected", "archived")
# An agent discard for a fact that can change does not reject the posting for good: it may reopen.
TRANSIENT_DISCARDS = frozenset({"closed_or_invalid"})


def profile_hash(profile: dict) -> str:
    """A digest of the profile's search criteria, stored with each verdict."""
    criteria = json.dumps((profile or {}).get("criteria") or {}, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(criteria.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def flags(conn: sqlite3.Connection, profile_slug: str, keys: Iterable[str]) -> dict[str, Flag]:
    """The memory flag of each identity key for ``profile_slug``.

    ``applied`` and ``rejected`` come from the user's review state (any profile), or from an agent discard
    for this profile. ``seen`` means an earlier run of this profile exported the posting. Everything else is
    ``new``, including postings seen only under another profile.
    """
    out: dict[str, Flag] = {}
    for key in dict.fromkeys(keys):
        out[key] = _flag(conn, profile_slug, key)
    return out


def _flag(conn: sqlite3.Connection, profile_slug: str, key: str) -> Flag:
    review = conn.execute("SELECT review_state FROM jobs WHERE identity_key = ?", (key,)).fetchone()
    if review and review[0] == "applied":
        return "applied"
    if review and review[0] in USER_REJECTED_STATES:
        return "rejected"
    verdict = conn.execute(
        "SELECT verdict, discard_category FROM scout_verdicts WHERE profile_slug = ? AND identity_key = ?",
        (profile_slug, key),
    ).fetchone()
    if verdict and verdict[0] == "discard" and verdict[1] not in TRANSIENT_DISCARDS:
        return "rejected"
    if verdict and verdict[0] in {"keep", "unsure"}:
        return "seen"
    exported = conn.execute(
        "SELECT 1 FROM job_occurrences o JOIN runs r ON r.run_id = o.run_id"
        " WHERE o.identity_key = ? AND r.profile_slug = ? LIMIT 1",
        (key, profile_slug),
    ).fetchone()
    return "seen" if exported else "new"






def record_verdicts(
    conn: sqlite3.Connection, profile_slug: str, run_id: str, verdicts: Iterable[dict], criteria_digest: str = "",
) -> int:
    """Store the agent verdicts of a finalized scout run; each later verdict replaces the earlier one.

    Each item has ``identity_key``, ``verdict``, ``discard_category``, ``priority``, ``content_hash``,
    ``company`` and ``row`` (the exported ``FinalRow``, or ``None`` for a discard). ``criteria_digest`` is the
    ``profile_hash`` of the criteria the verdicts were made under.
    """
    rows = [
        (profile_slug, item["identity_key"], item["verdict"], item.get("discard_category") or "",
         item.get("priority"), item.get("content_hash") or "", criteria_digest, item.get("company") or "",
         json.dumps(item["row"].model_dump(mode="json"), ensure_ascii=False) if item.get("row") else None,
         run_id, _now())
        for item in verdicts
    ]
    with conn:
        conn.executemany(
            "INSERT INTO scout_verdicts (profile_slug, identity_key, verdict, discard_category, priority,"
            " content_hash, profile_hash, company, row_json, run_id, recorded_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(profile_slug, identity_key) DO UPDATE SET verdict = excluded.verdict,"
            " discard_category = excluded.discard_category, priority = excluded.priority,"
            " content_hash = excluded.content_hash, profile_hash = excluded.profile_hash,"
            " company = excluded.company, row_json = excluded.row_json,"
            " run_id = excluded.run_id, recorded_at = excluded.recorded_at",
            rows,
        )
    return len(rows)
