"""Scout memory: what a profile already saw, rejected or applied to, and the agent's last verdicts.

A scout run reads this memory to brief the agent, to flag each find and to skip postings it already judged.
It stores the agent verdict of every evaluated posting per profile, with the exported row, so a later run can
carry an unchanged posting forward without asking the agent again.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Literal

from ultron.core.models import FinalRow

Flag = Literal["new", "seen", "rejected", "applied"]
USER_REJECTED_STATES = ("rejected", "archived")
# An agent discard for a fact that can change does not reject the posting for good: it may reopen.
TRANSIENT_DISCARDS = frozenset({"closed_or_invalid"})


def content_hash(title: str, location: str, remote_text: str = "", body_text: str = "") -> str:
    """A digest of the posting facts whose change makes the agent judge a seen posting again.

    It covers the title, location, remote policy and description, so a posting whose requirements or
    eligibility change is judged again instead of being carried from memory.
    """
    parts = (title, location, remote_text, body_text)
    text = " | ".join(" ".join(str(part or "").casefold().split()) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


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


def carried_row(conn: sqlite3.Connection, profile_slug: str, key: str, digest: str) -> tuple[FinalRow, int | None] | None:
    """The last exported row and priority of a seen posting whose facts did not change, else ``None``."""
    found = conn.execute(
        "SELECT verdict, content_hash, row_json, priority FROM scout_verdicts"
        " WHERE profile_slug = ? AND identity_key = ?",
        (profile_slug, key),
    ).fetchone()
    if not found or found[0] not in {"keep", "unsure"} or found[1] != digest or not found[2]:
        return None
    return FinalRow.model_validate(json.loads(found[2])), found[3]


def record_verdicts(conn: sqlite3.Connection, profile_slug: str, run_id: str, verdicts: Iterable[dict]) -> int:
    """Store the agent verdicts of a finalized scout run; each later verdict replaces the earlier one.

    Each item has ``identity_key``, ``verdict``, ``discard_category``, ``priority``, ``content_hash``,
    ``company`` and ``row`` (the exported ``FinalRow``, or ``None`` for a discard).
    """
    rows = [
        (profile_slug, item["identity_key"], item["verdict"], item.get("discard_category") or "",
         item.get("priority"), item.get("content_hash") or "", item.get("company") or "",
         json.dumps(item["row"].model_dump(mode="json"), ensure_ascii=False) if item.get("row") else None,
         run_id, _now())
        for item in verdicts
    ]
    with conn:
        conn.executemany(
            "INSERT INTO scout_verdicts (profile_slug, identity_key, verdict, discard_category, priority,"
            " content_hash, company, row_json, run_id, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(profile_slug, identity_key) DO UPDATE SET verdict = excluded.verdict,"
            " discard_category = excluded.discard_category, priority = excluded.priority,"
            " content_hash = excluded.content_hash, company = excluded.company, row_json = excluded.row_json,"
            " run_id = excluded.run_id, recorded_at = excluded.recorded_at",
            rows,
        )
    return len(rows)


def rejected_companies(conn: sqlite3.Connection, profile_slug: str, limit: int = 20) -> list[str]:
    """Employers whose postings the user rejected for this profile, most rejections first."""
    rows = conn.execute(
        "SELECT company, COUNT(*) AS n FROM jobs WHERE profile_slug = ? AND company != ''"
        " AND review_state IN (?, ?) GROUP BY company ORDER BY n DESC, company LIMIT ?",
        (profile_slug, *USER_REJECTED_STATES, limit),
    )
    return [company for company, _count in rows]


def known_count(conn: sqlite3.Connection, profile_slug: str) -> int:
    """Postings this profile already exported or judged."""
    exported = conn.execute(
        "SELECT COUNT(DISTINCT o.identity_key) FROM job_occurrences o JOIN runs r ON r.run_id = o.run_id"
        " WHERE r.profile_slug = ?",
        (profile_slug,),
    ).fetchone()[0]
    judged = conn.execute("SELECT COUNT(*) FROM scout_verdicts WHERE profile_slug = ?", (profile_slug,)).fetchone()[0]
    return max(exported, judged)
