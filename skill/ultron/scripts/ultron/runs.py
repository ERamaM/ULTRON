"""Run directories, stage bookkeeping and JSONL helpers."""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from ultron import home, presets, profiles

RUN_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}", re.ASCII)  # always used with fullmatch
STAGES = ("new", "seeds", "discover_feeds", "discover_ats", "candidates", "fetch", "filter", "evaluate", "finalize")
# A tuple value means "any of these stages must be done".
STAGE_REQUIRES: dict[str, str | tuple[str, ...]] = {
    "fetch": ("discover_ats", "discover_feeds"),
    "filter": "fetch",
    "finalize": "filter",
}


class StageOrderError(Exception):
    def __init__(self, stage: str, missing: str = ""):
        self.stage = stage
        self.missing = missing
        detail = f"stage {stage!r} requires {missing!r} to be done first" if missing else f"stage {stage!r} out of order"
        super().__init__(detail)


class RunNotFoundError(FileNotFoundError):
    pass


def _runs_root() -> Path:
    return home.ultron_home() / "runs"


def run_dir(run_id: str) -> Path:
    """Folder of ``run_id``; an id that is not ``YYYYMMDD-HHMMSS-xxxxxx`` raises ``RunNotFoundError``."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise RunNotFoundError(f"run not found: {run_id!r} (run ids look like YYYYMMDD-HHMMSS-xxxxxx)")
    return _runs_root() / run_id


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_run(run_id: str, data: dict) -> None:
    _atomic_write(run_dir(run_id) / "run.json", json.dumps(data, ensure_ascii=False, indent=2))


def new_run(profile_slug: str, preset: str, overrides: dict | None = None) -> str:
    settings = presets.resolve(preset)
    if overrides:
        settings.update(presets.validate_overrides(overrides, settings))
    snapshot = profiles.load_profile(profile_slug)
    now = datetime.now(UTC)
    run_id = f"{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
    run_dir(run_id).mkdir(parents=True)
    _write_run(run_id, {
        "run_id": run_id,
        "profile_slug": profile_slug,
        "preset": preset,
        "settings": settings,
        "profile_snapshot": snapshot,
        "stages_done": [],
        "created_at": now.isoformat(),
    })
    return run_id


def load_run(run_id: str) -> dict:
    path = run_dir(run_id) / "run.json"
    if not path.is_file():
        raise RunNotFoundError(f"run not found: {run_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_runs() -> list[dict]:
    out = []
    for path in sorted(_runs_root().glob("*/run.json"), reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append({k: data.get(k) for k in ("run_id", "profile_slug", "preset", "created_at", "stages_done")})
    return out


def mark_stage(run_id: str, stage: str) -> None:
    data = load_run(run_id)
    if stage not in data["stages_done"]:
        data["stages_done"].append(stage)
        _write_run(run_id, data)


def require_stage(run_id: str, stage: str) -> None:
    """Check that ``stage`` (the stage about to run) may run now.

    Dependencies (``STAGE_REQUIRES``): fetch needs discover_ats or discover_feeds (either one),
    filter needs fetch, finalize needs filter. Stages without a declared dependency are a no-op.
    Raises ``StageOrderError`` if no prerequisite is in ``stages_done``.
    """
    needed = STAGE_REQUIRES.get(stage)
    if needed is None:
        return
    options = (needed,) if isinstance(needed, str) else tuple(needed)
    done = load_run(run_id)["stages_done"]
    if not any(option in done for option in options):
        raise StageOrderError(stage, " or ".join(options))


def _dump(row: BaseModel | dict) -> str:
    data = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
    return json.dumps(data, ensure_ascii=False)


def write_jsonl(path: Path | str, rows: Iterable[BaseModel | dict]) -> int:
    lines = [_dump(row) for row in rows]
    _atomic_write(Path(path), "".join(line + "\n" for line in lines))
    return len(lines)


def append_jsonl(path: Path | str, rows: Iterable[BaseModel | dict]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [_dump(row) for row in rows]
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("".join(line + "\n" for line in lines))
    return len(lines)


def jsonl_lines(text: str) -> list[str]:
    """Split JSONL text into records on newlines only.

    ``str.splitlines()`` also breaks on U+2028, U+2029, U+0085 and other separators that
    ``json.dumps(ensure_ascii=False)`` leaves raw inside string values (posting bodies contain them).
    """
    return text.split("\n")


def read_jsonl(path: Path | str) -> list[dict]:
    path = Path(path)
    if not path.is_file():
        return []
    return [json.loads(line) for line in jsonl_lines(path.read_text(encoding="utf-8")) if line.strip()]
