"""No role-family or European vocabulary may be hardcoded in core Python code.

Role-family terms live in vocabulary packs; European geography rules live only in
``ultron/core/rules/geo_eu.py`` (applied only to European-scope profiles).
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import ultron.core

CORE = Path(ultron.core.__file__).resolve().parent
# Geographic rules for Europe are allowed to name European places.
EXCLUDED_FILES = {CORE / "rules" / "geo_eu.py"}
BANNED = re.compile(r"\b(?:machine[\s_-]+learning|llm|mlops|rag|spain|españa|espana)\b", re.IGNORECASE)
EXCEPTIONS: set[tuple[str, int]] = set()


def _offending_lines(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    hits: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE):
            continue
        if BANNED.search(token.string):
            rel = path.relative_to(CORE).as_posix()
            if (rel, token.start[0]) not in EXCEPTIONS:
                hits.append(f"{rel}:{token.start[0]}: {token.string.strip()[:80]}")
    return hits


def test_no_hardcoded_bias():
    files = [path for path in sorted(CORE.rglob("*.py")) if path not in EXCLUDED_FILES]
    assert files
    hits = [hit for path in files for hit in _offending_lines(path)]
    assert not hits, "hardcoded role vocabulary in core:\n" + "\n".join(hits)


def test_detector_ignores_comments_only():
    sample = '# machine learning is fine here\nX = "LLM"\n'
    hits = [
        token.string
        for token in tokenize.generate_tokens(io.StringIO(sample).readline)
        if token.type != tokenize.COMMENT and BANNED.search(token.string)
    ]
    assert hits == ['"LLM"']


def test_detector_bans_spain_but_not_identifiers():
    assert BANNED.search('"Remote from España"')
    assert BANNED.search("spain")
    assert not BANNED.search("remote_spain")
    assert not BANNED.search("SPAIN_NEGATION_TERMS")
