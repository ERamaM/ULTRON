"""Text helpers shared by the page extractors: whitespace, remote-work hints and joins."""

from __future__ import annotations

import re
from collections.abc import Iterable

from ultron.core.rules.hard_filters_data import STRONG_REMOTE_POSITIVE_TERMS

REMOTE_HINT_TERMS = ("remote", "remoto", "telecommute", "distributed", "work from home")
# Explicit workplace-conflict terms (hybrid/onsite). A bare "office" mention is NOT a
# workplace signal — incidental lines like "Optional office in Berlin" must not match.
REMOTE_CONFLICT_TERMS = ("hybrid", "on-site", "onsite", "in-office")
REMOTE_TECH_CONTEXT = (
    "remote monitoring",
    "remote access",
    "remote procedure",
    "remote sensing",
    "remote desktop",
    "remote connection",
    "distributed systems",
    "distributed computing",
    "distributed database",
    "distributed architecture",
)
STRONG_REMOTE_PHRASES = (*STRONG_REMOTE_POSITIVE_TERMS, "remote position", "work from home")


def collapse_whitespace(value: str) -> str:
    """Collapse consecutive whitespace runs in ``value`` to single spaces and strip ends."""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def same_text(left: str, right: str) -> bool:
    return collapse_whitespace(left).casefold() == collapse_whitespace(right).casefold()


def looks_remote(text: str) -> bool:
    """True only for genuine remote-work hints (no hybrid/onsite conflict terms)."""
    low = collapse_whitespace(text).lower()
    if not low:
        return False
    if any(token in low for token in REMOTE_TECH_CONTEXT):
        return False
    return any(token in low for token in REMOTE_HINT_TERMS)


def looks_remote_workplace(text: str) -> bool:
    """True for remote hints OR explicit hybrid/onsite conflict terms.

    Used where workplace-policy lines must be captured so downstream filters can
    reject hybrids; a bare "office" mention is intentionally not a signal.
    """
    low = collapse_whitespace(text).lower()
    if not low:
        return False
    if any(phrase in low for phrase in STRONG_REMOTE_PHRASES):
        return True
    if looks_remote(text):
        return True
    return any(token in low for token in REMOTE_CONFLICT_TERMS)


def dedupe_join(values: Iterable[str], *, limit: int = 4) -> str:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        clean = collapse_whitespace(value)
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(clean)
        if len(output) >= limit:
            break
    return " | ".join(output)

