"""Context-sensitive text signals used by the hard filters.

Each helper decides whether one lexical match is really the signal it resembles
("closed-loop" is not a closed posting, "Georgia" may be the country, "not remote"
is not a remote hint). They are pure functions over text so they stay testable.
"""

from __future__ import annotations

import re
from functools import lru_cache

from ultron.core.rules.hard_filters_data import (
    AGENCY_EXEMPTABLE_CLOSED_PHRASES,
    CLOSED_STATUS_AGENCY_SUFFIX_PATTERN,
    CLOSED_STATUS_PHRASES,
    REMOTE_HINT_TERMS,
)
from ultron.core.rules.term_matching import normalize_text

_REMOTE_HINT_NEGATION = re.compile(r"(?:\bnot-|\bnon-|\bisn't\b|\bnot\b|\bno\b)\s*$")
_REMOTE_BENEFIT_SUFFIX = re.compile(
    r"^\s+(?:work\s+)?(?:reimbursement|stipend|allowance|budget|equipment)\b",
    re.IGNORECASE,
)
_DISTRIBUTED_TEAM_HINT = re.compile(r"\bdistributed teams?\b", re.IGNORECASE)
_GEORGIA_COUNTRY_CUES = re.compile(r"\b(?:tbilisi|batumi|caucasus)\b")
_GEORGIA_US_CUES = re.compile(r"\b(?:atlanta|savannah|ga|usa|united states)\b")


CLOSED_STATUS_AGENCY_SUFFIX_RE = re.compile(CLOSED_STATUS_AGENCY_SUFFIX_PATTERN, re.IGNORECASE)
_TYPOGRAPHIC_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})
# Every phrase at every position (zero-width lookahead), longest first, in one scan.
_CLOSED_STATUS_PHRASES_RE = re.compile(
    r"(?<![^\W_])(?=("
    + "|".join(re.escape(phrase) for phrase in sorted(CLOSED_STATUS_PHRASES, key=lambda p: (-len(p), p)))
    + r")(?![^\W_]))"
)
# "Applications accepted until the position has been filled" describes the future, not a closed posting.
_CLOSED_STATUS_CONDITION_BEFORE_RE = re.compile(
    r"\b(?:until|till|once|when|whenever|if|after|unless|before|hasta que|una vez que|cuando|sobald|bis|wenn"
    r"|jusqu'a ce que|des que|lorsque|quand)\b[^.;:!?\n]{0,40}$"
)
# French "pourvu de/d'" means "provided with" ("le poste est pourvu d'un vehicule").
_PROVIDED_WITH_AFTER_RE = re.compile(r"\s+(?:d'|de\b|du\b|des\b)")


@lru_cache(maxsize=64)
def _term_pattern(term: str) -> re.Pattern[str]:
    return re.compile(r"\b" + re.escape(term) + r"\b", re.IGNORECASE)


def georgia_window_is_us_state(window: str) -> bool:
    """True when the text around "Georgia" points at the US state, not the country."""
    low = window.lower()
    if _GEORGIA_COUNTRY_CUES.search(low):
        return False
    if _GEORGIA_US_CUES.search(low):
        return True
    return re.search(r"\bu\.s\.", low) is not None


def closed_match_is_technical_or_negated(low: str, match: re.Match[str]) -> bool:
    """True for "closed-loop" style compounds and for "not closed" / "never closed"."""
    suffix = low[match.end() : match.end() + 32]
    if len(suffix) > 1 and suffix[0] == "-" and suffix[1].isalpha():
        return True
    prefix = low[max(0, match.start() - 45) : match.start()]
    return re.search(r"\b(?:not|isn't|never)\s+$", prefix) is not None


def has_remote_work_hint(text: str) -> bool:
    """True when the text names remote work, ignoring "not remote" and bare "distributed"."""
    for term in REMOTE_HINT_TERMS:
        pattern = _DISTRIBUTED_TEAM_HINT if term == "distributed team" else _term_pattern(term)
        for match in pattern.finditer(text):
            if term == "remote":
                prefix = text[max(0, match.start() - 24) : match.start()]
                if _REMOTE_HINT_NEGATION.search(prefix):
                    continue
                suffix = text[match.end() : match.end() + 40]
                if _REMOTE_BENEFIT_SUFFIX.search(suffix):
                    continue
            return True
    return False


def has_closed_status_phrase(text: str) -> bool:
    """Whole-phrase ``CLOSED_STATUS_PHRASES`` match, accent- and apostrophe-insensitive.

    "Not accepting applications from agencies" style boilerplate does not count (see
    ``AGENCY_EXEMPTABLE_CLOSED_PHRASES``), nor does a phrase in a condition ("until the position has been
    filled") or French "pourvu de" (provided with). Shared by the hard filters and the extractor's
    ``is_open_hint``.
    """
    low = normalize_text(text).translate(_TYPOGRAPHIC_APOSTROPHES)
    for match in _CLOSED_STATUS_PHRASES_RE.finditer(low):
        phrase = match.group(1)
        end = match.start() + len(phrase)
        if phrase in AGENCY_EXEMPTABLE_CLOSED_PHRASES and CLOSED_STATUS_AGENCY_SUFFIX_RE.match(low, end):
            continue
        if _CLOSED_STATUS_CONDITION_BEFORE_RE.search(low, max(0, match.start() - 60), match.start()):
            continue
        if phrase.endswith(("pourvu", "pourvue")) and _PROVIDED_WITH_AFTER_RE.match(low, end):
            continue
        return True
    return False
