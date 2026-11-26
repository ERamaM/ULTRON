"""Find the publication date of a posting in free text.

The first date in a page is often a cookie banner or "last updated" stamp, so a date
next to a publication label wins, and dates labelled as updates or deadlines are skipped.
"""

from __future__ import annotations

import re
from datetime import date, datetime

DATE_RE = re.compile(
    r"(20\d{2}[-/](?:0?[1-9]|1[0-2])[-/](?:0?[1-9]|[12]\d|3[01])\b"
    r"|\b\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}\b)"
)
_POSTING_DATE_LABEL_RE = re.compile(
    r"date posted|fecha de publicaci[oó]n|\bposted\b|\bpublished\b|\bpublicado\b",
    re.IGNORECASE,
)
_NON_POSTING_DATE_LABEL_RE = re.compile(
    r"valid until|\bupdated\b|\bmodified\b|\bactualizado\b|\bexpires\b|\bdeadline\b",
    re.IGNORECASE,
)


def _same_line_prefix(text: str, start: int, limit: int) -> str:
    line_start = max(text.rfind("\n", 0, start), text.rfind("\r", 0, start)) + 1
    prefix = text[line_start:start]
    if len(prefix) <= limit:
        return prefix
    return prefix[-limit:]


def posting_date_from_text(text: str, *, require_label: bool = False) -> str | None:
    """Return the publication date found in ``text``, or None."""
    matches = list(DATE_RE.finditer(text))
    for match in matches:
        if _POSTING_DATE_LABEL_RE.search(_same_line_prefix(text, match.start(), 40)):
            return match.group(1)
    if require_label:
        return None
    for match in matches:
        if _NON_POSTING_DATE_LABEL_RE.search(_same_line_prefix(text, match.start(), 30)):
            continue
        return match.group(1)
    return None


def iso_vacancy_date(value: object) -> str | None:
    """Return the calendar date of an ISO date or timestamp."""
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][^\r\n]{1,32})?", value):
        return None
    try:
        return (datetime.fromisoformat(value).date() if len(value) > 10 else date.fromisoformat(value)).isoformat()
    except ValueError:
        return None
