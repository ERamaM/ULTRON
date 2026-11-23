"""Check that measurable draft claims preserve the source role and outcome."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from ultron.core.privacy import redact
from ultron.core.rules.term_matching import contains_term, normalize_text
from ultron.cv import experience
from ultron.cv.ats_target import KNOWN_SKILL_TERMS

_ROLE_SEPARATOR = re.compile(r"\s(?:at|en)\s|[|·]", re.IGNORECASE)
_SENTENCE = re.compile(r"(?<=[.!?])[ \t]+(?=[A-Z])|\r?\n")
_CAUSAL = re.compile(r"\b(?:using|with|through|via|mediante|usando|con)\b")
_OUTCOMES = {
    "cost": re.compile(r"\b(?:costs?|expenses?|spend|costes?|costos?|gastos?)\b"),
    "latency": re.compile(r"\b(?:latency|response times?|latencia|tiempos? de respuesta)\b"),
    "revenue": re.compile(r"\b(?:revenue|sales|ingresos|ventas)\b"),
    "throughput": re.compile(r"\b(?:throughput|rendimiento|requests per second)\b"),
    "users": re.compile(r"\b(?:users?|customers?|usuarios?|clientes?)\b"),
}
_DIRECTIONS = {
    "decrease": re.compile(r"\b(?:reduc\w{0,8}|cut|lower\w{0,5}|decreas\w{0,5}|ahorr\w{0,5}|disminu\w{0,5})\b"),
    "increase": re.compile(r"\b(?:increas\w{0,5}|grew|grow\w{0,5}|rais\w{0,5}|aument\w{0,5}|creci\w{0,5})\b"),
}
SAFE_EXPANSION_PAIRS: tuple[tuple[str, str], ...] = (
    ("ml", "machine learning"),
    ("ai", "artificial intelligence"),
    ("nlp", "natural language processing"),
    ("llm", "large language model"),
    ("llms", "large language models"),
    ("rag", "retrieval augmented generation"),
    ("rag", "retrieval-augmented generation"),
    ("k8s", "kubernetes"),
    ("ci/cd", "continuous integration"),
    ("js", "javascript"),
    ("ts", "typescript"),
    ("sql", "structured query language"),
    ("etl", "extract transform load"),
    ("elt", "extract load transform"),
    ("aws", "amazon web services"),
    ("gcp", "google cloud platform"),
    ("api", "application programming interface"),
    ("rest", "representational state transfer"),
    ("oop", "object oriented programming"),
    ("oop", "object-oriented programming"),
    ("tdd", "test driven development"),
    ("tdd", "test-driven development"),
    ("bi", "business intelligence"),
    ("qa", "quality assurance"),
    ("ui", "user interface"),
    ("ux", "user experience"),
    ("db", "database"),
    ("dl", "deep learning"),
)

_CANONICAL_METHODS = {
    **{normalize_text(term): normalize_text(term) for term in KNOWN_SKILL_TERMS},
    **{term: short for short, full in SAFE_EXPANSION_PAIRS for term in (short, full)},
}
Metrics = Callable[[str], Iterable[str]]


@dataclass(frozen=True)
class ClaimScope:
    """The identified employer and role of a statement, if present."""

    employer: str = ""
    role: str = ""


@dataclass(frozen=True)
class Claim:
    """An original sentence carrying a recognized metric and its attributed evidence."""

    text: str
    scope: ClaimScope
    outcomes: set[str]
    directions: set[str]
    methods: set[str]


def _blocks(text: str, ref: date) -> list[tuple[ClaimScope, str]]:
    sections, _ = experience.split_sections(text)
    work = sections.get("experience", [])
    entries = experience.parse_date_ranges_from_lines(work, as_of=ref)
    if not entries:
        return [(ClaimScope(), text)]
    out = [(ClaimScope(), "\n".join(
        line for name, lines in sections.items() if name != "experience" for line in lines
    ))]
    for index, entry in enumerate(entries):
        parts = [normalize_text(part.strip()) for part in _ROLE_SEPARATOR.split(entry.title_hint) if part.strip()]
        scope = ClaimScope(parts[-1], parts[0]) if len(parts) >= 2 else ClaimScope()
        end = entries[index + 1].heading_line_index if index + 1 < len(entries) else len(work)
        out.append((scope, "\n".join(work[entry.line_index + 1:end])))
    return out


def _named_scope(fragment: str, default: ClaimScope, employers: set[str]) -> ClaimScope:
    matches = {employer for employer in employers if contains_term(fragment, employer)}
    if len(matches) == 1:
        employer = next(iter(matches))
        return default if employer == default.employer else ClaimScope(employer)
    return default


def _claims(blocks: list[tuple[ClaimScope, str]], employers: set[str], metrics: Metrics) -> dict[str, list[Claim]]:
    found: dict[str, list[Claim]] = {}
    for default, body in blocks:
        for sentence in _SENTENCE.split(body):
            metric_keys = set(metrics(sentence))
            if not metric_keys:
                continue
            text = normalize_text(sentence)
            scope = _named_scope(text, default, employers)
            causal = _CAUSAL.search(text)
            claim = Claim(
                sentence.strip(), scope,
                {name for name, pattern in _OUTCOMES.items() if pattern.search(text)},
                {name for name, pattern in _DIRECTIONS.items() if pattern.search(text)},
                {canonical for term, canonical in _CANONICAL_METHODS.items()
                 if causal and contains_term(text[causal.end():], term)},
            )
            for key in metric_keys:
                found.setdefault(key, []).append(claim)
    return found


def _supports(source: Claim, draft: Claim) -> bool:
    old, new = source.scope, draft.scope
    if new.employer and new.employer != old.employer:
        return False
    if new.role and old.role and not (contains_term(new.role, old.role) or contains_term(old.role, new.role)):
        return False
    if source.outcomes and draft.outcomes and not (source.outcomes & draft.outcomes):
        return False
    if source.directions and draft.directions and not (source.directions & draft.directions):
        return False
    return draft.methods <= source.methods


def verify_claim_attribution(
    source: str, draft: str, confirmed: list[str], *, as_of: date, metrics: Metrics,
) -> tuple[list[dict], list[dict]]:
    """Return unsupported claims and statements whose source role cannot be determined."""
    source_blocks = _blocks(source, as_of)
    draft_blocks = _blocks(draft, as_of)
    employers = {scope.employer for scope, _body in (*source_blocks, *draft_blocks) if len(scope.employer) > 2}
    authorized = _claims(source_blocks + [(ClaimScope(), fact) for fact in confirmed], employers, metrics)
    proposed = _claims(draft_blocks, employers, metrics)
    flagged = {}
    ambiguous = {}
    for key, candidates in proposed.items():
        matching = authorized.get(key, [])
        for claim in candidates:
            if not matching:
                continue
            if any(_supports(record, claim) for record in matching):
                if not claim.scope.employer and len({record.scope.employer for record in matching} - {""}) > 1:
                    ambiguous[claim.text] = "Several source roles contain this metric. Confirm which role supports the statement."
                continue
            if claim.scope.employer and all(not record.scope.employer for record in matching):
                ambiguous[claim.text] = "The source contains the metric without an identifiable employer."
                continue
            flagged[claim.text] = "The source does not link this metric to the stated role, outcome or method."
    return (
        [{"type": "unsupported_claim_attribution", "severity": "high", "token": str(redact(text[:300])),
          "message": message} for text, message in flagged.items()],
        [{"type": "ambiguous_claim_attribution", "severity": "medium", "token": str(redact(text[:300])),
          "message": message} for text, message in ambiguous.items() if text not in flagged],
    )