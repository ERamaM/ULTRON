from __future__ import annotations

import pytest

from ultron.core.text_clean import clean_text


@pytest.mark.parametrize("raw,expected", [
    ("R&amp;D Engineer", "R&D Engineer"),
    ("Senior Engineer &#8211; Remote", "Senior Engineer – Remote"),
    ("R&amp;amp;D", "R&D"),
    ("España", "España"),
    ("EspaÃ±a", "España"),
    ("Engineerâ€™s", "Engineer’s"),
    ("São Paulo", "São Paulo"),
    ("GERENTE NÃO", "GERENTE NÃO"),
    ("  a \t b\n c  ", "a b c"),
    ("Café", "Café"),
    ("", ""),
])
def test_clean_text(raw, expected):
    assert clean_text(raw) == expected


@pytest.mark.parametrize("raw", ["R&amp;amp;D", "EspaÃ±a", "Engineerâ€™s", "São Paulo", "x &#8211; y"])
def test_idempotent(raw):
    once = clean_text(raw)
    assert clean_text(once) == once


def test_never_raises_on_non_str():
    assert clean_text(None) == ""  # type: ignore[arg-type]
    assert clean_text(42) == "42"  # type: ignore[arg-type]
