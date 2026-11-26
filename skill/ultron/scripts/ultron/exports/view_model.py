"""Display rules shared by workbook, offline HTML, and web ledger exports."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping
from hashlib import sha256
from statistics import median
from typing import Any

from ultron.core.extractors.posting_date import iso_vacancy_date
from ultron.exports import sanitize
from ultron.salary.display import format_salary_disclosure

__all__ = [
    "MATCH_BANDS", "best_vacancy_date", "company_monogram", "detect_export_mode",
    "format_salary_disclosure", "normalize_priority", "parse_salary_midpoint",
    "priority_label", "remote_label", "split_fit_signals", "summarize_job_display",
]

MATCH_BANDS = ("strong", "good", "borderline", "ineligible")
_PRIORITY_LABELS = {1: "P1 · Apply first", 2: "P2 · Strong", 3: "P3 · Backup"}
_REMOTE_LABELS = dict(zip(
    ("remote_spain", "remote_eu", "remote_europe", "remote_emea", "remote_other", "hybrid", "onsite"),
    ("Remote · Spain", "Remote · EU", "Remote · Europe", "Remote · EMEA", "Remote · Other", "Hybrid", "On-site"),
    strict=True,
))
_DATE_FIELDS = (
    ("published_at", "published", "Published"),
    ("updated_at", "updated", "Updated"),
    ("first_seen", "first_seen", "First seen"),
    ("unverified_date", "unverified", "Unverified date"),
    ("freshness", "unverified", "Unverified date"),
)
_AMOUNT = r"(?:[0-9]{1,3}(?:,[0-9]{3}){1,3}|[0-9]{1,12})(?:\.[0-9]{1,2})?"
_ANNUAL_SALARY = re.compile(
    rf"([A-Z]{{3}}) ({_AMOUNT})(?:[\u2013-]({_AMOUNT}))?[ ]{{0,3}}(?:/[ ]{{0,3}}year|per year|annually)",
    re.IGNORECASE | re.ASCII,
)


def normalize_priority(value: object) -> int | None:
    """Return an integer priority from one to three if valid."""
    return int(value) if type(value) in {int, str} and str(value) in {"1", "2", "3"} else None


def priority_label(value: object) -> str:
    """Return the display label for a known priority."""
    return _PRIORITY_LABELS.get(normalize_priority(value), "")


def detect_export_mode(rows: Iterable[Mapping[str, Any]]) -> str:
    """Classify an export by the presence of sweep scores and unscored toolkit rows."""
    scored = toolkit = False
    for row in rows:
        has_score = sanitize.clamp_score(row.get("match_score")) is not None
        scored |= has_score or sanitize.normalize_token(row.get("match_band"), MATCH_BANDS) in MATCH_BANDS
        toolkit |= not has_score and (
            normalize_priority(row.get("priority")) is not None or row.get("found_by") == "agent_verified"
        )
    if scored and toolkit:
        return "mixed"
    return "toolkit" if toolkit else "scored"


def best_vacancy_date(row: Mapping[str, Any]) -> dict[str, str] | None:
    """Choose the first valid display date in provenance order."""
    for field, kind, label in _DATE_FIELDS:
        if iso := iso_vacancy_date(row.get(field)):
            return {"iso": iso, "kind": kind, "label": f"{label} {iso}"}
    return None


def company_monogram(company: object) -> dict[str, Any]:
    """Create up to two alphanumeric initials and a stable palette index."""
    name = unicodedata.normalize("NFC", sanitize.clean_text(company))
    words = ["".join(ch for ch in word if ch.isalnum()) for word in name.split()]
    words = [word for word in words if word]
    initials = (words[0][:2] if len(words) == 1 else "".join(word[0] for word in words[:2])).upper()[:2]
    return {"initials": initials or "?", "tone": sha256(name.casefold().encode()).digest()[0] % 8}


def split_fit_signals(value: object, max_tags: int = 8) -> list[str]:
    """Sanitize, deduplicate and bound fit-signal tags."""
    parts = re.split(r"[,|]", value) if isinstance(value, str) else value
    if not isinstance(parts, (list, tuple)) or max_tags <= 0:
        return []
    tags: dict[str, str] = {}
    for part in parts:
        if isinstance(part, str) and (tag := sanitize.clean_text(part)[:60].rstrip()):
            tags.setdefault(tag.casefold(), tag)
            if len(tags) >= max_tags:
                break
    return list(tags.values())


def remote_label(remote_policy: object) -> str:
    """Return the label of a recognized remote policy."""
    return _REMOTE_LABELS.get(sanitize.clean_text(remote_policy).lower(), "")


def parse_salary_midpoint(salary_text: object) -> tuple[str, float] | None:
    """Extract an unambiguous annual midpoint without converting currencies or periods."""
    match = _ANNUAL_SALARY.fullmatch(sanitize.clean_text(salary_text)[:121])
    if not match:
        return None
    currency, lower, upper = match.groups()
    low = float(lower.replace(",", ""))
    high = float((upper or lower).replace(",", ""))
    return (currency.upper(), (low + high) / 2) if 0 < low <= high else None


def summarize_job_display(jobs: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate priority, remote policies and comparable annual disclosures."""
    priorities = dict.fromkeys(("p1", "p2", "p3", "unset"), 0)
    remote: Counter[str] = Counter()
    salaries: dict[str, list[float]] = {}
    disclosed = 0
    for job in jobs:
        priority = normalize_priority(job.get("priority"))
        priorities[f"p{priority}" if priority else "unset"] += 1
        remote[job.get("remote_policy") or "unknown"] += 1
        if job.get("salary"):
            disclosed += 1
            if parsed := parse_salary_midpoint(job["salary"]):
                currency, midpoint = parsed
                salaries.setdefault(currency, []).append(midpoint)
    return {
        "display_mode": detect_export_mode(jobs),
        "priority_counts": priorities,
        "remote_counts": dict(sorted(remote.items())),
        "salary_summary": {
            "disclosed_count": disclosed,
            "by_currency": {
                currency: {"count": len(values), "min": min(values), "median": median(values), "max": max(values)}
                for currency, values in sorted(salaries.items())
            },
        },
    }
