"""Bidirectional title-synonym expansion for role focus terms."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

# Generic groups only; family-specific groups come from the active vocabulary pack
# (``VocabularyPack.title_synonym_groups``).
TITLE_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("data scientist", "cientifico de datos"),
    ("data engineer", "ingeniero de datos"),
    ("software engineer", "software developer", "ingeniero de software", "desarrollador de software"),
    (
        "materials engineer", "materials and processes engineer", "materials and process engineer",
        "materials & processes engineer",
        "materials process engineer", "ingeniero de materiales", "ingeniera de materiales",
    ),
    (
        "process engineer", "process engineering", "ingeniero de procesos", "ingeniera de procesos",
        "ingenieria de procesos",
    ),
    ("manufacturing engineer", "ingeniero de fabricacion", "ingeniera de fabricacion"),
)


def normalize_role_title(title: str) -> str:
    """Keep the written gender when a Spanish title appends its alternative (ingeniero/a)."""
    return re.sub(r"(?<=\w)(o/a|a/o)\b", lambda match: match[0][0], _normalize_term(title))


def _normalize_term(term: str) -> str:
    folded = unicodedata.normalize("NFKD", str(term or ""))
    return "".join(ch for ch in folded if not unicodedata.combining(ch)).strip().lower()


def expand_role_terms(
    terms: Iterable[str] | None, *, extra_groups: Sequence[Sequence[str]] = ()
) -> list[str]:
    """Expand focus terms with known synonyms (generic plus ``extra_groups``), preserving order."""
    groups = (*TITLE_SYNONYM_GROUPS, *(tuple(group) for group in extra_groups))
    expanded: list[str] = []
    seen: set[str] = set()

    def _add(term: str) -> None:
        low = _normalize_term(term)
        if low and low not in seen:
            seen.add(low)
            expanded.append(low)

    for term in terms or ():
        low = _normalize_term(str(term))
        _add(low)
        for group in groups:
            if low in group:
                for synonym in group:
                    _add(synonym)
    return expanded
