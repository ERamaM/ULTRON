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
from ultron.stages.scout_verified import ELIGIBILITIES, EXPORTED_VERDICTS, REMOTE_POLICIES, final_row, load_verified
from ultron.storage import maintenance, runs
from ultron.toolkit.dossier import employer_name
from ultron.toolkit.inspect import board_aliases, cached_postings

MAX_ROWS = 200
FILLED_FIELDS = ("title", "company", "location")


_WORK_MODE_ALIASES = {"on_site": "onsite", "on_premises": "onsite", "in_office": "onsite",
                      "hybrid_remote": "hybrid"}


def _judgment_fields(row: dict) -> dict:
    """Normalize optional work-mode and eligibility strings without inventing evidence."""
    fields = {}
    for name, allowed in (("remote_policy", REMOTE_POLICIES), ("eligibility", ELIGIBILITIES)):
        if isinstance(value := row.get(name), str):
            token = "_".join(value[:100].strip().casefold().replace("-", " ").split())
            if name == "remote_policy":
                token = _WORK_MODE_ALIASES.get(token, token)
            fields[name] = token if token in allowed else "unknown"
    return {**row, **fields}


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
        url = cached.get("url") or job.get("apply_url", "")
        defaults = {"url": url, **{field: job.get(field, "") for field in FILLED_FIELDS}}
        defaults["company"] = employer_name(defaults["company"], url)  # never empty or the platform's name
        merged = {**defaults, **{key: value for key, value in row.items() if value not in (None, "")}}
        if "published_at" not in row:
            if job.get("posting_date_verified") is True:
                merged["published_at"] = job.get("posting_date")
            else:
                merged.setdefault("unverified_date", job.get("posting_date"))
        merged.setdefault("updated_at", job.get("updated_at"))
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
    summary with the output files. An all-invalid submission leaves no run and returns a null run ID.
    """
    if not rows:
        raise EmptyShortlist("the shortlist has no rows")
    rows = list(rows)
    over = [{"row": number, "outcome": "over_limit", "reason": f"only the first {MAX_ROWS} rows are saved per call"}
            for number in range(MAX_ROWS + 1, len(rows) + 1)]
    cache = cached_postings(profile_slug)
    rows = [_judgment_fields(row) if isinstance(row, dict) else row for row in rows[:MAX_ROWS]]
    rows = _with_cache(rows, cache)
    run_id = runs.new_run(profile_slug, preset, mode="scout")
    run = runs.load_run(run_id)
    # Board context comes only from ULTRON's own cache, never from the rows, so a row cannot claim an identity.
    added = add_scout_finds(run_id, run, rows, boards=board_aliases(cache))
    not_saved = [row for row in added["rows"] if row["outcome"] != "verified"] + over
    if not sum(added["verified"].values()):
        maintenance.delete_run(run_id, yes=True)
        return {"run_id": None, "saved": added["verified"], "not_saved": not_saved,
                "roles": 0, "new_roles": 0, "unsure": 0, "discarded": 0,
                "final_cap": 0, "verified_finds": 0, "outputs": {}, "top": []}
    _credit_boards(run_id)
    for stage in ("fetch", "filter"):
        runs.mark_stage(run_id, stage)
    result = finalize_scout(run_id, runs.load_run(run_id))
    return {"run_id": run_id, "saved": added["verified"], "not_saved": not_saved, **result}
