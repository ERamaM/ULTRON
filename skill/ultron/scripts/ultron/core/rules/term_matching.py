"""Small, accent-insensitive word-boundary term matching helpers."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable


def normalize_text(text: str) -> str:
    """Unicode-fold accents and lowercase ``text`` for rule matching."""
    normalized = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def contains_term(text: str, term: str) -> bool:
    """Return True if normalized ``term`` matches within normalized ``text``."""
    normalized_term = normalize_text(term.strip())
    if not normalized_term:
        return False
    return re.search(r"(?<!\w)" + re.escape(normalized_term) + r"(?!\w)", normalize_text(text)) is not None


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    """Return ordered unique terms from ``terms`` that match in ``text``."""
    found: list[str] = []
    seen: set[str] = set()
    normalized = normalize_text(text)
    for term in terms:
        normalized_term = normalize_text(term.strip())
        if (
            term not in seen
            and normalized_term
            and re.search(r"(?<!\w)" + re.escape(normalized_term) + r"(?!\w)", normalized)
        ):
            found.append(term)
            seen.add(term)
    return found


def matching_term_span(text: str, terms: Iterable[str], *, max_length: int) -> tuple[int, int] | None:
    """Return the first matching term's span in the original, unnormalized text."""
    folded = []
    original_offsets = []
    for offset, char in enumerate(text):
        for part in unicodedata.normalize("NFKD", char):
            if not unicodedata.combining(part):
                folded.append(part)
                original_offsets.append(offset)
    normalized = "".join(folded).lower()
    normalized_offsets = [offset for char, offset in zip(folded, original_offsets, strict=True) for _ in char.lower()]
    first: tuple[int, int] | None = None
    for term in terms:
        normalized_term = normalize_text(term.strip())
        if not normalized_term:
            continue
        for match in re.finditer(r"(?<!\w)" + re.escape(normalized_term) + r"(?!\w)", normalized):
            span = (normalized_offsets[match.start()], normalized_offsets[match.end() - 1] + 1)
            end = span[1]
            while end < len(text) and unicodedata.combining(text[end]):
                end += 1
            span = (span[0], end)
            if span[1] - span[0] > max_length:
                continue
            if first is None or span[0] < first[0]:
                first = span
            break
    return first


def extract_matching_evidence(text: str, terms: Iterable[str]) -> str:
    """Return a bounded source window containing an actual configured term."""
    # A term absent from the whole normalized text cannot match any sentence: drop it once up front, so
    # large term lists (closed-status phrases) do not cost one regex scan per term per sentence.
    normalized_text = normalize_text(text)
    terms = tuple(term for term in terms if normalize_text(term.strip()) in normalized_text)
    if not terms:
        return ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        sentence = sentence.strip()
        span = matching_term_span(sentence, terms, max_length=280)
        if span is None:
            continue
        _, end = span
        window = sentence[max(0, end - 280) : max(280, end)]
        if find_terms(window, terms):
            return window
    return ""
