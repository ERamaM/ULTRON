"""Extract explicit CV requirement groups and preserve their posting evidence."""

from __future__ import annotations

import re

from ultron.core.privacy import redact
from ultron.core.rules.term_matching import contains_term, normalize_text

_BOUNDARY = re.compile(r"\r?\n|;|[.!?](?=\s)")
_REQUIRED = re.compile(
    r"\b(?:required|requirements|must have|mandatory|essential|requisitos?|obligatori[oa]s?|imprescindibles?)\b"
)
_PREFERRED = re.compile(
    r"\b(?:preferred|nice to have|desirable|a plus|deseables?|valorables?|se valorara|preferibles?)\b"
)
_NEGATED = re.compile(r"\b(?:not|no)\b")
_ALTERNATIVE = re.compile(r"\b(?:or|o)\b")
_CONJUNCTION = re.compile(r"\b(?:and|y)\b|,")


def _category(text: str, heading: str) -> str:
    required = bool(_REQUIRED.search(text))
    preferred = bool(_PREFERRED.search(text))
    if (required and preferred) or (_NEGATED.search(text) and (required or preferred or heading != "unspecified")):
        return "unspecified"
    if required:
        return "required"
    if preferred:
        return "preferred"
    return heading


def _groups(clause: str, terms: list[str]) -> list[tuple[str, ...]]:
    groups = []
    for segment in _CONJUNCTION.split(clause):
        options = _ALTERNATIVE.split(segment)
        hits = [tuple(term for term in terms if contains_term(option, term)) for option in options]
        if len(hits) > 1 and all(len(hit) == 1 for hit in hits):
            groups.append(tuple(dict.fromkeys(hit[0] for hit in hits)))
        else:
            groups.extend((term,) for term in terms if any(term in hit for hit in hits))
    return groups


def classify_requirements(body: str, terms: list[str]) -> list[dict]:
    """Return source-backed required, preferred and unspecified term groups."""
    found: dict[tuple, dict] = {}
    heading = "unspecified"
    for line in body.splitlines():
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.endswith(":") and len(stripped) <= 80:
            heading = _category(normalize_text(stripped), "unspecified")
            continue
        for fragment in _BOUNDARY.split(stripped):
            clause = normalize_text(fragment)
            category = _category(clause, heading)
            for group in _groups(clause, terms):
                found.setdefault((category, group), {
                    "category": category,
                    "alternatives": list(group),
                    "evidence": str(redact(fragment.strip()[:500])),
                })
    return list(found.values())