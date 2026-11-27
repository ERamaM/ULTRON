"""JSON output helpers (leaf module: command modules import from here, not ultron.cli)."""

from __future__ import annotations

import json
import sys


def emit(payload: dict) -> None:
    """Print a single JSON object to stdout (UTF-8, no ASCII escaping)."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False) + "\n")


def fail(error: str, detail: str = "", code: int = 2) -> int:
    """Print a JSON error object and return a non-zero exit code."""
    emit({"error": error, "detail": detail})
    return code


def fail_stage_order(exc: Exception) -> int:
    """Map a ``runs.StageOrderError`` to the JSON error contract (shared by all stage commands)."""
    return fail("stage_order", str(exc), code=3)
