"""``ultron shortlist save``: persist the agent's ranked shortlist with ULTRON's memory, history and exports.

Each row is a posting the agent judged: a verified find (specification 30) with ``verdict`` and ``priority``. A
row may name a posting ``inspect`` read by its ``id`` alone: its URL, title, company and location come from the
inspect cache. Saving creates a scout run, records every row as a verified find and finalizes it, so the
shortlist gets ``results.md`` / ``results.json``, the ledger's history and new-since-last-run flags, the scout
memory (discards are never shown again) and watchlist credit for the boards of kept postings.
"""

from __future__ import annotations

from typing import Any

from ultron.core import presets
from ultron.salary.display import format_salary_disclosure
from ultron.stages.discover import SEED_ATTEMPTS_FILE
from ultron.stages.final_helpers import row_board_key
from ultron.stages.scout_finalize import finalize_scout
from ultron.stages.scout_finds import add_scout_finds
from ultron.stages.scout_verified import EXPORTED_VERDICTS, final_row, load_verified
from ultron.storage import runs
from ultron.toolkit.inspect import board_aliases, cached_postings

MAX_ROWS = 200
FILLED_FIELDS = ("title", "company", "location")


class EmptyShortlist(ValueError):
    """``shortlist save`` got no rows."""


def _with_cache(rows: list, cache: dict[str, dict]) -> list:
    """Fill the URL, title, company and location of rows that name an inspected posting by ``id``."""
    filled = []
    for row in rows:
        if not isinstance(row, dict):
            filled.append(row)
            continue
        cached = cache.get(str(row.get("id") or ""))
        if cached is None:
            filled.append(row)
            continue
        job = cached["job"]
        defaults = {"url": cached.get("url") or job.get("apply_url", ""),
                    **{field: job.get(field, "") for field in FILLED_FIELDS}}
        merged = {**defaults, **{key: value for key, value in row.items() if value not in (None, "")}}
        if "published_at" not in row:
            merged["published_at"] = job.get("posting_date")
        if "salary" not in row:
            merged["salary"] = job.get("salary") or format_salary_disclosure(job.get("body_text", ""))
        filled.append(merged)
    return filled


def _credit_boards(run_id: str) -> None:
    """Record the ATS boards of kept postings as queried, so their keeps reach the watchlist."""
    keys = set()
    for found in load_verified(runs.run_dir(run_id)).values():
        if found["verdict"] in EXPORTED_VERDICTS and (key := row_board_key(final_row(found))):
            keys.add(key)
    runs.write_jsonl(runs.run_dir(run_id) / SEED_ATTEMPTS_FILE, [{"seed_key": key} for key in sorted(keys)])


def save_shortlist(profile_slug: str, rows: list, *, preset: str = presets.DEFAULT_PRESET) -> dict[str, Any]:
    """Save ``rows`` (the agent's judged postings) as a finalized shortlist for ``profile_slug``.

    Returns the run ID, the rows that were not saved (``invalid``, ``already_rejected``...) and the finalize
    summary with the output files.
    """
    if not rows:
        raise EmptyShortlist("the shortlist has no rows")
    rows = list(rows)
    over = [{"row": number, "outcome": "over_limit", "reason": f"only the first {MAX_ROWS} rows are saved per call"}
            for number in range(MAX_ROWS + 1, len(rows) + 1)]
    cache = cached_postings(profile_slug)
    rows = _with_cache(rows[:MAX_ROWS], cache)
    run_id = runs.new_run(profile_slug, preset, mode="scout")
    run = runs.load_run(run_id)
    # Board context comes only from ULTRON's own cache, never from the rows, so a row cannot claim an identity.
    added = add_scout_finds(run_id, run, rows, boards=board_aliases(cache))
    _credit_boards(run_id)
    for stage in ("fetch", "filter"):
        runs.mark_stage(run_id, stage)
    result = finalize_scout(run_id, runs.load_run(run_id))
    not_saved = [row for row in added["rows"] if row["outcome"] != "verified"] + over
    return {"run_id": run_id, "saved": added["verified"], "not_saved": not_saved, **result}
