"""Saved searches: named (profile, preset, overrides) combinations, runnable unattended with a digest."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime

from ultron import ledger, presets, profiles, runs
from ultron.stages.unattended import run_unattended


def create(name: str, profile_slug: str, preset: str = presets.DEFAULT_PRESET, prompt: str = "",
           overrides: dict | None = None) -> None:
    name = name.strip()
    if not name:
        raise ValueError("saved search name must not be empty")
    try:
        presets.resolve(preset)
    except KeyError as exc:
        raise ValueError(str(exc.args[0])) from exc
    overrides = presets.validate_overrides(overrides or {})
    profiles.load_profile(profile_slug)  # raises FileNotFoundError if missing
    now = datetime.now(UTC).isoformat()
    with closing(ledger.connect()) as conn, conn:
        conn.execute(
            "INSERT INTO saved_searches (name, profile_slug, preset, prompt, overrides_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET profile_slug=excluded.profile_slug, "
            "preset=excluded.preset, prompt=excluded.prompt, overrides_json=excluded.overrides_json, "
            "updated_at=excluded.updated_at",
            (name, profile_slug, preset, prompt, json.dumps(overrides, sort_keys=True), now, now),
        )


_COLS = "name, profile_slug, preset, prompt, overrides_json, created_at, updated_at"


def _row(r: tuple) -> dict:
    return {"name": r[0], "profile_slug": r[1], "preset": r[2], "prompt": r[3],
            "overrides": json.loads(r[4] or "{}"), "created_at": r[5], "updated_at": r[6]}


def list_all() -> list[dict]:
    with closing(ledger.connect()) as conn:
        return [_row(r) for r in conn.execute(f"SELECT {_COLS} FROM saved_searches ORDER BY name")]


def get(name: str) -> dict:
    with closing(ledger.connect()) as conn:
        row = conn.execute(f"SELECT {_COLS} FROM saved_searches WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise LookupError(f"saved search not found: {name}")
    return _row(row)


def delete(name: str) -> bool:
    with closing(ledger.connect()) as conn, conn:
        return conn.execute("DELETE FROM saved_searches WHERE name = ?", (name,)).rowcount > 0


def write_digest(run_id: str, name: str) -> tuple[str, int]:
    """Write ``runs/<id>/digest.md`` listing only the offers new in this run; return (path, count)."""
    path = runs.run_dir(run_id) / "results.json"
    rows = json.loads(path.read_text(encoding="utf-8"))["rows"] if path.is_file() else []
    new = [r for r in rows if r.get("is_new")]
    lines = [f"# Digest: {name}", "", f"Run {run_id}: {len(new)} new role(s).", ""]
    for r in new:
        lines.append(f"- {r.get('title', '')} - {r.get('company', '')} - {r.get('apply_url', '')} "
                     f"(score {r.get('match_score', '')})")
    out = runs.run_dir(run_id) / "digest.md"
    runs._atomic_write(out, "\n".join(lines) + "\n")
    return str(out), len(new)


def run(name: str, *, unattended: bool) -> dict:
    from ultron.runs_cmd import build_query_plan

    s = get(name)
    overrides = s["overrides"] or None
    if unattended:
        result = run_unattended(s["profile_slug"], s["preset"], overrides)
        digest, count = write_digest(result["run_id"], name)
        return {"run_id": result["run_id"], "new_roles": count, "digest": digest}
    run_id = runs.new_run(s["profile_slug"], s["preset"], overrides)
    runs.mark_stage(run_id, "new")
    data = runs.load_run(run_id)
    return {"run_id": run_id, "prompt": s["prompt"],
            "query_plan": build_query_plan(data["profile_snapshot"], data["settings"]["max_search_queries"])}
