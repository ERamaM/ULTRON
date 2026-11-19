"""Conservative, dimension-specific proof of explicit incompatibility."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence

from ultron.core.models import ExtractedJob
from ultron.core.rules.term_matching import normalize_text

_AMBIGUOUS = re.compile(
    r"\b(?:not|no|never|without|optional|opcional|alternatively|alternative|or|o|sin|except|excepto|"
    r"isn['’]t|aren['’]t|doesn['’]t|won['’]t)\b"
)
_REMOTE_PATTERNS = (
    r"hybrid\s*,?\s*[1-9]\d*\s+days?\s+(?:in|at)\s+(?:the\s+)?office",
    r"mandatory\s+hybrid\s+(?:work|working)",
    r"(?:three|two|one|[1-9]\d*)\s+office\s+days?\s+(?:are\s+)?required",
    r"(?:on[ -]?site|in[ -]office)\s+(?:attendance|work|presence)\s+(?:is\s+)?(?:required|mandatory)",
    r"(?:presencialidad|asistencia presencial)\s+(?:es\s+)?(?:obligatoria|requerida)",
    r"(?:trabajo|puesto|puestos|modalidad)\s+(?:hibrido|hibrida|hibridos|hibridas|presencial|presenciales)"
    r"(?:\s*,?\s*(?:[1-9]\d*|uno|dos|tres|cuatro|cinco)\s+dias?\s+en\s+oficina)?",
)
_SENIORITY_PATTERN = (
    r"(?:(?:junior|intern|entry[ -]level|becario|becaria|practicas)\s+"
    r"(?:[\w-]+\s+){0,4}(?:engineer|developer|analyst|ingeniero|ingeniera|desarrollador|desarrolladora|analista)"
    r"|(?:ingeniero|ingeniera|desarrollador|desarrolladora|analista)\s+junior)"
)
_CLOSED = frozenset(
    {
        "applications closed",
        "application closed",
        "posting closed",
        "role closed",
        "position filled",
        "job expired",
        "vacante cerrada",
        "oferta cerrada",
        "plaza cubierta",
        "inscripcion cerrada",
    }
)


def evidence_covers_source(dimension: str, evidence: str, job: ExtractedJob | None) -> bool:
    """Prove the whole relevant context, not an isolated sentence within it.

    Additional uninterpreted text is unknown. This deliberately sacrifices
    recall rather than assuming that later context cannot revoke a statement.
    """
    if job is None or not evidence.strip():
        return False

    def canonical(value: str) -> str:
        return re.sub(r"\s+", " ", unicodedata.normalize("NFC", value).casefold()).strip()

    fields = {
        "role": (job.title, job.body_text),
        "seniority": (job.title, job.body_text),
        "remote": (job.remote_text, job.body_text),
        "open_status": (job.body_text,),
    }.get(dimension, ())
    target = canonical(evidence)
    if not target:
        return False
    context = [canonical(field) for field in fields if field and field.strip()]
    return bool(context) and all(field == target for field in context)


def explicitly_contradicts(dimension: str, evidence: str, *, role_terms: Sequence[str] = ()) -> bool:
    """Only accept bounded, unambiguous statements; unknown is the safe fallback."""
    text = re.sub(r"\s+", " ", normalize_text(evidence)).strip().rstrip(".! ")
    if not text:
        return False
    if dimension == "role":
        # One denied target is insufficient when the profile accepts alternatives.
        if any(not isinstance(term, str) or not term.strip() for term in role_terms):
            return False
        terms = {normalize_text(term).strip() for term in role_terms}
        if len(terms) != 1:
            return False
        target = next(iter(terms))
        if len(target) > 100 or _AMBIGUOUS.search(target) or "\n" in target or "\r" in target:
            return False
        term = re.escape(target)
        return re.fullmatch(rf"(?:this is not an? {term} role|no es un puesto de {term})", text) is not None
    if _AMBIGUOUS.search(text):
        return False
    if dimension == "remote":
        return any(re.fullmatch(pattern, text) is not None for pattern in _REMOTE_PATTERNS)
    if dimension == "seniority":
        if re.search(r"\b(?:senior|staff|principal|lead)\b", text):
            return False
        return re.fullmatch(_SENIORITY_PATTERN, text) is not None
    if dimension == "open_status":
        return text in _CLOSED
    return False
