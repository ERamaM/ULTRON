from __future__ import annotations

import re

from ultron.core.rules.classification_signals import EXPERIENCE_SIGNAL_PATTERNS


def _matches(text: str) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in EXPERIENCE_SIGNAL_PATTERNS)


def test_three_plus_years_counts():
    assert _matches("3+ years of experience with Python")


def test_spanish_years_count():
    assert _matches("5 años de experiencia en machine learning")
    assert _matches("10+ años de experiencia")


def test_plain_three_years_does_not_count():
    assert not _matches("3 years of experience")


def test_range_still_excluded():
    assert not _matches("3-5 years of experience")
