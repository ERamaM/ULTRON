"""Shared CLI command helpers: date parsing and document verification/build error mapping."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date
from typing import Any

from ultron.commands.output import emit, fail
from ultron.cv import docx_writer as cv_docx_writer
from ultron.cv import extract as cv


def parse_as_of(raw: str | None) -> date | None:
    """Parse an optional ``--as-of YYYY-MM-DD`` option string."""
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ValueError(f"invalid --as-of date {raw!r}: expected YYYY-MM-DD") from exc


def run_verify_truth(fn: Callable[[], dict[str, Any]], *, strict: bool) -> int:
    """Execute a truthfulness verification callable and map standard errors to CLI exit codes."""
    try:
        report = fn()
    except cv.CvExtractionError as exc:
        return fail(exc.code, str(exc))
    except FileNotFoundError as exc:
        return fail("not_found", str(exc))
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_input", str(exc))
    emit(report)
    return 1 if (strict and not report["passed"]) else 0


def run_build_docx(fn: Callable[[], dict[str, Any]]) -> int:
    """Execute a ``.docx`` build callable and map standard errors to CLI exit codes."""
    try:
        payload = fn()
    except cv_docx_writer.TruthfulnessError as exc:
        emit({"error": "truthfulness_failed", "detail": str(exc), "truth": exc.truth_report})
        return 1
    except cv.CvExtractionError as exc:
        return fail(exc.code, str(exc))
    except FileExistsError as exc:
        return fail("output_exists", str(exc))
    except FileNotFoundError as exc:
        return fail("not_found", str(exc))
    except (OSError, UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        return fail("invalid_input", str(exc))
    emit(payload)
    return 0
