"""Input validation for the arm comparison: shortlist rows, JSON files and run timestamps."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from ultron.core.privacy import redact
from ultron.storage.runs import jsonl_lines

ROW_FIELDS = ("title", "company", "apply_url", "location")
MAX_TEXT_CHARS = 500
MAX_URL_CHARS = 2048


class ComparisonError(ValueError):
    """Invalid session, input file, labels, or an incomplete experiment."""


def _text_field(raw: dict[str, Any], field: str, index: int) -> str:
    value = raw.get(field)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ComparisonError(f"row {index}: {field} must be a string")
    return " ".join(value.split())


def _check_url(url: str, index: int) -> None:
    if len(url) > MAX_URL_CHARS:
        raise ComparisonError(f"row {index}: apply_url is longer than {MAX_URL_CHARS} characters")
    try:
        parts = urlsplit(url)
        host = parts.hostname
    except ValueError as exc:
        raise ComparisonError(f"row {index}: apply_url is malformed") from exc
    if parts.scheme.lower() not in {"http", "https"} or not host:
        raise ComparisonError(f"row {index}: apply_url must be an http(s) URL")
    if parts.username or parts.password:
        raise ComparisonError(f"row {index}: apply_url must not embed a user name or password")
    if redact(url) != url:
        raise ComparisonError(f"row {index}: apply_url carries a credential-like value; remove it first")


def clean_row(raw: Any, index: int) -> dict[str, str]:
    """One shortlist row in the common format; ``ComparisonError`` when it is unusable."""
    if not isinstance(raw, dict):
        raise ComparisonError(f"row {index}: expected a JSON object")
    row = {field: _text_field(raw, field, index) for field in ROW_FIELDS}
    if not row["title"] or not row["apply_url"]:
        raise ComparisonError(f"row {index}: title and apply_url are required")
    _check_url(row["apply_url"], index)
    for field in ("title", "company", "location"):
        row[field] = row[field][:MAX_TEXT_CHARS]
    return row


def run_seconds(summary: dict[str, Any]) -> float | None:
    """Seconds between a run summary's ``started_at`` and ``ended_at`` (``None`` if missing or negative)."""
    try:
        start = datetime.fromisoformat(str(summary["started_at"]))
        end = datetime.fromisoformat(str(summary["ended_at"]))
        seconds = (end - start).total_seconds()
    except (KeyError, TypeError, ValueError):  # TypeError: one naive and one aware timestamp
        return None
    return round(seconds, 1) if seconds >= 0 else None


def json_object(text: str | None, what: str) -> dict[str, Any]:
    """``text`` parsed as a JSON object; ``ComparisonError`` naming ``what`` otherwise."""
    try:
        data = json.loads(text or "")
    except ValueError as exc:
        raise ComparisonError(f"{what} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ComparisonError(f"{what} is not a JSON object")
    return data


def parse_jsonl(text: str) -> list[Any]:
    """The JSON values of a JSONL text, blank lines skipped."""
    rows = []
    for number, line in enumerate(jsonl_lines(text), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as exc:
            raise ComparisonError(f"line {number}: invalid JSON") from exc
    return rows
