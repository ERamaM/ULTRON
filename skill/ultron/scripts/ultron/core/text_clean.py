"""Display-text normalisation for titles, companies and locations (ULTRON stage boundaries only)."""

from __future__ import annotations

import html
import unicodedata

_MOJIBAKE_MARKERS = ("Ã", "â€", "Â")


def _marks(s: str) -> int:
    return sum(s.count(m) for m in _MOJIBAKE_MARKERS)


def _fix_mojibake(s: str) -> str:
    if not _marks(s):
        return s
    for codec in ("cp1252", "latin-1"):
        try:
            fixed = s.encode(codec).decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if _marks(fixed) < _marks(s):
            return fixed
    return s


def clean_text(s: str) -> str:
    """Unescape HTML entities, repair UTF-8-read-as-cp1252 mojibake, collapse whitespace, NFC. Idempotent, never raises."""
    try:
        if s is None:
            return ""
        text = s if isinstance(s, str) else str(s)
        for _ in range(2):
            unescaped = html.unescape(text)
            if unescaped == text:
                break
            text = unescaped
        text = _fix_mojibake(text)
        text = " ".join(text.split())
        return unicodedata.normalize("NFC", text)
    except Exception:  # noqa: BLE001 - cleaning must never break a stage
        return s if isinstance(s, str) else ""
