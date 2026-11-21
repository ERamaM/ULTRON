"""Literal substring and word-boundary matching, without normalization policy."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=1024)
def word_boundary_pattern(term: str) -> re.Pattern:
    """Compile a case-insensitive word-boundary regex for ``term``."""
    return re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)


def contains_any_substring(text: str, terms: Iterable[str]) -> bool:
    """Return True if any lowercase substring in ``terms`` appears in ``text``."""
    text_low = text.lower()
    return any(term in text_low for term in terms)


def contains_any_word_boundary(text: str, terms: Iterable[str]) -> bool:
    """Return True if any term in ``terms`` matches on word boundaries in ``text``."""
    return any(word_boundary_pattern(term).search(text) for term in terms)


@lru_cache(maxsize=1024)
def term_boundary_pattern(term: str) -> re.Pattern:
    """Match ``term`` only when it is not glued to letters or digits on either side.

    Unlike a word-boundary regex, this also works for terms that start or end with punctuation, such as ``u.s. (remote)``.
    Underscores count as separators, so ``_us-based_`` still matches.
    """
    return re.compile(r"(?<![^\W_])" + re.escape(term) + r"(?![^\W_])", re.IGNORECASE)


def contains_any_term_boundary(text: str, terms: Iterable[str]) -> bool:
    """Return True if any term in ``terms`` matches with delimiter-aware boundaries."""
    return any(term_boundary_pattern(term).search(text) for term in terms)
