"""Canonical dependency-free redaction boundary for the scraping engine.

This module intentionally remains independent from :mod:`mark_iii.settings.privacy`:
the engine can run as a standalone package, so it provides environment/token
redaction only and does not own MARK III's SQLite-backed remote-consent state.
The application layer uses ``mark_iii.settings.privacy`` for consent and the same
redaction contract at its persistence boundaries.
"""

from __future__ import annotations

import dataclasses
import os
import re
from typing import Any

REDACTED = "[REDACTED]"
_SENSITIVE_NAME = re.compile(
    r"(?:api[_-]?key|access[_-]?token|auth(?:orization)?|bearer|secret|password|passwd|cookie|set-cookie|proxy-authorization|private[_-]?key|credential)",
    re.IGNORECASE,
)
# Candidate matching is deliberately simple (single linear quantifiers, no nested lookaheads) so
# hostile multi-kilobyte text cannot trigger backtracking; the "does this look like a credential"
# shape checks run afterwards in ``_token_replacement``.
_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:"
    r"(?i:bearer)\s+(?P<bearer>[A-Za-z0-9._~+/=-]{16,})"
    r"|(?i:basic)\s+(?P<basic>[A-Za-z0-9+/]{12,}={0,2})(?![A-Za-z0-9+/=-])"
    r"|sk-ant-[A-Za-z0-9_-]{8,}"
    r"|sk-[A-Za-z0-9_-]{20,}"
    r"|hf_[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9_-]{8,}"
    r"|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|AIza[A-Za-z0-9_-]{8,}"
    r")"
)
_JWT_SHAPE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_CASE_TRANSITION = re.compile(r"[a-z][0-9+/]*[A-Z]")


def _has_word_slash_word(token: str) -> bool:
    """Three letters on both sides of a ``/`` (``English/Spanish``), checked in linear time."""
    start = token.find("/")
    while start != -1:
        before, after = token[max(0, start - 3) : start], token[start + 1 : start + 4]
        if len(before) == 3 and len(after) == 3 and before.isalpha() and after.isalpha() and before.isascii() and after.isascii():
            return True
        start = token.find("/", start + 1)
    return False


def _looks_like_basic_credential(token: str) -> bool:
    if _has_word_slash_word(token):
        return False
    return (
        any(ch.isdigit() for ch in token)
        or token.endswith("=")
        or len(_CASE_TRANSITION.findall(token)) >= 2
    )


def _token_replacement(match: re.Match[str]) -> str:
    bearer = match.group("bearer")
    if bearer is not None:
        if (len(bearer) >= 16 and _JWT_SHAPE.match(bearer)) or any(ch.isdigit() for ch in bearer):
            return REDACTED
        return match.group(0)
    basic = match.group("basic")
    if basic is not None:
        return REDACTED if _looks_like_basic_credential(basic) else match.group(0)
    text = match.group(0)
    if text.startswith("sk-"):
        return REDACTED if any(ch.isdigit() for ch in text) else text
    return REDACTED


_URL_SECRET_QUERY = re.compile(r"([?&](?:api[_-]?key|token|access[_-]?token|secret|password)=)([^&#]*)", re.IGNORECASE)
# Greenhouse embed links (``boards.greenhouse.io/embed/job_app?for=acme&token=4567``) carry the public,
# numeric job id in ``token``; redacting it breaks the exported apply link. Only a numeric ``token`` inside
# a greenhouse.io URL is kept; every other ``token`` value stays redacted.
_GREENHOUSE_URL_PREFIX = re.compile(r"https?://(?:[a-z0-9-]+\.)*greenhouse\.io(?::\d+)?/[^\s\"'<>]*", re.IGNORECASE)
_URL_PREFIX_SCAN_LIMIT = 2048
# Characters that can never appear inside a URL, so they always end one.
_URL_HARD_BOUNDARY_CHARS = " \t\r\n\"'<>"
# Characters that wrap links in prose (``(url)``, ``[text](url)``) but are also legal inside a URL.
_URL_SOFT_BOUNDARY_CHARS = "()[]"
# The id must end the URL: digits, then optional closing brackets or sentence punctuation (legal URL characters,
# so they only count when nothing but the end of the value or a hard boundary follows them).
_NUMERIC_URL_VALUE = re.compile(r"[0-9]+(?=[)\].,;:!?]*(?:[\s\"'<>]|$))")


def _greenhouse_job_token(match: re.Match[str]) -> str | None:
    """The numeric job id of a ``token`` parameter inside a greenhouse.io URL, else ``None``."""
    prefix, value = match.group(1), match.group(2)
    if prefix[1:-1].casefold() != "token":
        return None
    numeric = _NUMERIC_URL_VALUE.match(value)
    if numeric is None:
        return None
    start = match.start()
    window_start = max(0, start - _URL_PREFIX_SCAN_LIMIT)
    window = match.string[window_start:start]
    token_start = max(window.rfind(ch) for ch in _URL_HARD_BOUNDARY_CHARS) + 1
    if token_start == 0 and window_start > 0:
        return None  # the containing URL starts before the scanned window: its host is unknown
    url_start = max(token_start, max(window.rfind(ch, token_start) for ch in _URL_SOFT_BOUNDARY_CHARS) + 1)
    if "/" in window[token_start:url_start]:
        return None  # the bracket sits inside another URL's path (``https://other.example/(https://...``)
    return numeric.group(0) if _GREENHOUSE_URL_PREFIX.fullmatch(window, url_start) else None


def _redact_url_secrets(text: str) -> str:
    # The value group can run past the URL into following prose. For a kept job id only the id itself is
    # copied, and scanning resumes right after it, so later URLs in the same text are still redacted.
    parts: list[str] = []
    pos = 0
    while (match := _URL_SECRET_QUERY.search(text, pos)) is not None:
        job_id = _greenhouse_job_token(match)
        if job_id is None:
            parts.append(text[pos : match.start()] + match.group(1) + REDACTED)
            pos = match.end()
        else:
            end = match.start(2) + len(job_id)
            parts.append(text[pos:end])
            pos = end
    parts.append(text[pos:])
    return "".join(parts)


def environment_secret_values() -> tuple[str, ...]:
    """Return configured credential values without importing the UI layer."""

    names = (
        "MARK_III_LLM_API_KEY",
        "OPENROUTER_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "MARK_III_HF_TOKEN",
        "HF_TOKEN",
        "HUGGINGFACE_TOKEN",
    )
    return tuple(value for name in names if (value := os.getenv(name, "")))


def _redact_string(value: str, secret_values: tuple[str, ...]) -> str:
    text = str(value)
    for secret in sorted((item for item in secret_values if item), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    text = _redact_url_secrets(text)
    return _TOKEN_PATTERN.sub(_token_replacement, text)


def redact(value: Any, *, secret_values: tuple[str, ...] = ()) -> Any:
    """Recursively remove credentials from engine-owned diagnostics/exports."""

    secrets = tuple(secret_values) + environment_secret_values()
    if isinstance(value, dict):
        return {
            key: REDACTED if _SENSITIVE_NAME.search(str(key)) else redact(item, secret_values=secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item, secret_values=secrets) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, secret_values=secrets) for item in value)
    if isinstance(value, set):
        return {redact(item, secret_values=secrets) for item in value}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return redact(dataclasses.asdict(value), secret_values=secrets)
    if isinstance(value, str):
        return _redact_string(value, secrets)
    return value


def redact_exception(exc: BaseException, *, secret_values: tuple[str, ...] = ()) -> str:
    """Return a stable, secret-free exception message for engine diagnostics."""
    return str(redact(str(exc), secret_values=secret_values))
