"""Literal substring and word-boundary matching, without normalization policy."""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=1024)
def word_boundary_pattern(term: str) -> re.Pattern:
    return re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)


def contains_any_substring(text: str, terms: Iterable[str]) -> bool:
    text_low = text.lower()
    return any(term in text_low for term in terms)


def contains_any_word_boundary(text: str, terms: Iterable[str]) -> bool:
    return any(word_boundary_pattern(term).search(text) for term in terms)
