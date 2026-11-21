"""Page-title cleanup: strip the site or careers name that pages append after a separator."""

from __future__ import annotations

import re
from collections.abc import Iterable
from itertools import islice

from ultron.core.extractors.text import clean_text, same_text

# Page titles (<title>, og:title, twitter:title) often append the site or careers name after a separator:
# "Data Engineer | Lead Your Journey - with Acme." or "Data Engineer - Acme Careers".
PAGE_TITLE_SOURCES = frozenset({"meta_og", "meta_twitter", "title_tag"})
_STRONG_TITLE_SEPARATOR_RE = re.compile(r"\s+(?:\||::|·|•)\s+")
_DASH_TITLE_SEPARATOR_RE = re.compile(r"\s+[-–—]\s+")
# Page titles come from untrusted pages: a longer title, or more separators than this, is returned as is
# instead of being split (each split copies and compares the text, so the work stays bounded).
_MAX_STRIPPABLE_TITLE_CHARS = 300
_MAX_TITLE_SEPARATORS = 16
_SITE_SUFFIX_CAREER_WORDS = frozenset({
    "career", "careers", "job", "jobs", "hiring", "vacancies", "openings", "karriere", "stellenangebote",
    "empleo", "empleos", "carreras", "emplois", "vacatures", "lavora",
})
# Words that may sit around a site name in a suffix ("Jobs at Acme", "with Acme", "Acme & Co").
_SITE_SUFFIX_FILLER_WORDS = frozenset({"at", "with", "by", "the", "and", "in", "for", "of", "bei", "en", "chez", "de"})


def _title_words(value: str) -> list[str]:
    return re.findall(r"\w+", clean_text(value).casefold())


def _site_name_phrases(site_names: Iterable[str]) -> list[list[str]]:
    """Each site name as a word list without careers words ("Acme Career" -> ["acme"])."""
    phrases: list[list[str]] = []
    for name in site_names:
        words = [word for word in _title_words(name) if word not in _SITE_SUFFIX_CAREER_WORDS]
        if words and len("".join(words)) >= 3 and words not in phrases:
            phrases.append(words)
    return phrases


def _phrase_spans(words: list[str], phrase: list[str]) -> list[range]:
    size = len(phrase)
    return [range(i, i + size) for i in range(len(words) - size + 1) if words[i : i + size] == phrase]


def _site_like_suffix(suffix: str, phrases: list[list[str]], *, strict: bool) -> bool:
    """True when ``suffix`` names the site: its name or a careers word.

    ``strict`` (dash separators, which also join legitimate title parts such as "Engineer - Payments")
    requires every word to be the site name, a careers word or a filler word; otherwise the suffix only
    has to contain the site name or end with a careers word.
    """
    words = _title_words(suffix)
    if not words:
        return False
    site_positions = {i for phrase in phrases for span in _phrase_spans(words, phrase) for i in span}
    if not strict:
        return bool(site_positions) or words[-1] in _SITE_SUFFIX_CAREER_WORDS
    if not site_positions and not any(word in _SITE_SUFFIX_CAREER_WORDS for word in words):
        return False
    return all(
        i in site_positions or word in _SITE_SUFFIX_CAREER_WORDS or word in _SITE_SUFFIX_FILLER_WORDS
        for i, word in enumerate(words)
    )


def strip_page_title_suffix(title: str, *, anchors: Iterable[str], site_names: Iterable[str]) -> str:
    """Drop a site or careers suffix from a page title; the title is returned unchanged without one.

    A prefix equal to a trusted title (JSON-LD, h1, ATS hint) wins first. Otherwise the earliest strong
    separator (``|``) whose suffix names the site is cut, then the earliest dash whose suffix is only the
    site or careers name.
    """
    clean = clean_text(title)
    if not clean or len(clean) > _MAX_STRIPPABLE_TITLE_CHARS:
        return clean
    phrases = _site_name_phrases(site_names)
    # An anchor that is only the site name or a careers word ("<h1>Acme</h1>", "<h1>Careers</h1>") is no title.
    trusted = [
        clean_text(anchor)
        for anchor in anchors
        if (words := [w for w in _title_words(anchor) if w not in _SITE_SUFFIX_CAREER_WORDS]) and words not in phrases
    ]
    strong = list(islice(_STRONG_TITLE_SEPARATOR_RE.finditer(clean), _MAX_TITLE_SEPARATORS))
    dashes = list(islice(_DASH_TITLE_SEPARATOR_RE.finditer(clean), _MAX_TITLE_SEPARATORS))
    matches = sorted([*strong, *dashes], key=lambda m: m.start())
    for match in matches:
        prefix = clean[: match.start()].strip()
        if prefix and any(same_text(prefix, anchor) for anchor in trusted):
            return prefix
    for separators, strict in ((strong, False), (dashes, True)):
        for match in separators:
            prefix = clean[: match.start()].strip()
            if len(prefix) >= 3 and _site_like_suffix(clean[match.end() :], phrases, strict=strict):
                return prefix
    return clean

