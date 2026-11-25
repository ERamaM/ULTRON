"""Session files of an arm comparison: names, the private reports folder and the saved state."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from arm_inputs import ComparisonError
from safe_paths import UnsafePathError, read_report, write_report

SCHEMA_VERSION = 1
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}")
WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def session_name(raw: str) -> str:
    """``raw`` when it is a valid session name; ``ComparisonError`` otherwise."""
    if not isinstance(raw, str) or not NAME_RE.fullmatch(raw) or raw.lower() in WINDOWS_RESERVED:
        raise ComparisonError(f"{raw!r}: session names use letters, digits, '-' and '_' (max 48)")
    return raw


def artifact(session: str, kind: str) -> str:
    """The reports-folder file name of one session artifact (``state.json``, ``labels.yaml``...)."""
    return f"{session_name(session)}.{kind}"


def read_text(name: str, *, missing_ok: bool = False) -> str | None:
    """The text of a reports-folder file; ``None`` for a missing one when ``missing_ok``."""
    try:
        return read_report(name)
    except UnsafePathError as exc:
        raise ComparisonError(str(exc)) from exc
    except FileNotFoundError as exc:
        if missing_ok:
            return None
        raise ComparisonError(f"{name}: not found in the reports folder") from exc
    except (OSError, UnicodeDecodeError) as exc:
        raise ComparisonError(f"{name}: unreadable ({type(exc).__name__})") from exc


def write_text(name: str, text: str) -> Path:
    """Write a reports-folder file (redacted, never through a link) and return its path."""
    try:
        return write_report(name, text)
    except UnsafePathError as exc:
        raise ComparisonError(str(exc)) from exc


def load_state(session: str) -> dict[str, Any]:
    """The saved state of ``session`` (raises ``ComparisonError`` before ``prepare``)."""
    text = read_text(artifact(session, "state.json")) or ""
    try:
        state = json.loads(text)
    except ValueError as exc:
        raise ComparisonError(f"{session}: the state file is not valid JSON") from exc
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION:
        raise ComparisonError(f"{session}: unsupported state file")
    return state


def save_state(state: dict[str, Any]) -> Path:
    """Write ``state`` back to its session file."""
    text = json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False)
    return write_text(artifact(state["session"], "state.json"), text)
