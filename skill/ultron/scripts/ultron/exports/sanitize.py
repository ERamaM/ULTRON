"""Shared text redaction, score clamping, and HTTP(S) URL sanitization for exports."""

from __future__ import annotations

import math
import unicodedata
from typing import Any
from urllib.parse import urlsplit

from ultron.core.privacy import redact


def _strip_controls(text: str, *, preserve_newlines: bool) -> str:
    if preserve_newlines:
        cleaned = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
        return "".join(ch for ch in cleaned if ch == "\n" or unicodedata.category(ch) not in {"Cc", "Cf"})
    cleaned = text.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    return "".join(ch for ch in cleaned if unicodedata.category(ch) not in {"Cc", "Cf"})


def clean_text(value: Any) -> str:
    """Normalize ``value`` to a single redacted line stripped of control/format chars."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(item) for item in value if item not in (None, ""))
    stripped = _strip_controls(str(value), preserve_newlines=False)
    # Redact after stripping zero-width characters that could split a token ("gh\u200bp_...").
    return str(redact(stripped)).strip()


def clean_multiline_text(value: Any) -> str:
    """Sanitize and redact multiline notes while preserving LF line breaks."""
    if value is None:
        return ""
    stripped = _strip_controls(str(value), preserve_newlines=True)
    return str(redact(stripped)).strip()


def safe_http_url(value: Any) -> str | None:
    """Return a validated raw ``http(s)`` URL with a hostname, or None if unsafe."""
    if not isinstance(value, str):
        return None
    url = clean_text(value)
    if not url or url != value.strip() or any(ch.isspace() for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return url


def clamp_score(value: Any) -> int | None:
    """Clamp numeric match scores into ``0..100``, returning None for invalid inputs."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return max(0, min(100, round(value)))


def normalize_token(value: Any, allowed: tuple[str, ...], *, default: str = "none") -> str:
    """Return lowercase ``value`` when present in ``allowed``, otherwise ``default``."""
    text = str(value or "").strip().lower()
    return text if text in allowed else default
