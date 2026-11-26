"""Validate authenticated local requests and delegate ledger mutations to storage."""

from __future__ import annotations

import hmac
import json
import sqlite3
from contextlib import closing
from email.message import Message
from http import HTTPStatus
from pathlib import Path
from threading import Lock
from typing import BinaryIO
from urllib.parse import urlsplit

import yaml

from ultron.core.ats.board_key import resolve_board_key
from ultron.exports import sanitize
from ultron.profiles import store
from ultron.storage import ledger, review, watchlist

MAX_BODY = 16_384
_FIELDS = {
    "/api/jobs/state": ({"identity_key", "state"}, {}),
    "/api/jobs/notes": ({"identity_key", "text"}, {}),
    "/api/jobs/feedback": ({"identity_key", "relevance"}, {"reason_code": "", "note": "", "profile": ""}),
    "/api/watchlist/add": ({"profile", "board"}, {}),
    "/api/watchlist/remove": ({"profile", "seed_key"}, {}),
}
_LIMITS = {"identity_key": 2048, "text": 2000, "note": 2000, "profile": 128, "board": 1024, "seed_key": 1024,
           "state": 32, "relevance": 32, "reason_code": 32}


class MutationError(ValueError):
    """A rejected edit with a safe response message and HTTP status."""

    def __init__(self, status: HTTPStatus, reason: str) -> None:
        """Record the transport status without retaining the input."""
        super().__init__(reason)
        self.status = status


def _header(headers: Message, name: str) -> str | None:
    values = headers.get_all(name) or []
    return values[0] if len(values) == 1 else None


def valid_host(headers: Message, authority: str) -> bool:
    """Check that the request names the exact loopback authority of this session."""
    host = _header(headers, "Host")
    return host is not None and host.lower() == authority


class LaunchGrant:
    """One-use bootstrap capability shared by all requests to one server."""

    def __init__(self, capability: str) -> None:
        """Retain the capability only in memory until the browser pairs."""
        self._expected = capability.encode("ascii")
        self._lock = Lock()

    def consume(self, headers: Message) -> bool:
        """Atomically consume a matching launch capability once."""
        supplied = (_header(headers, "X-ULTRON-Launch") or "").encode("utf-8")
        with self._lock:
            if not self._expected or not hmac.compare_digest(supplied, self._expected):
                return False
            self._expected = b""
            return True


def authorize(headers: Message, origin: str, token: str) -> int:
    """Authorize an edit before reading its body and return the allowed byte count."""
    submitted = _header(headers, "X-ULTRON-Token") or ""
    if not hmac.compare_digest(submitted.encode("utf-8"), token.encode("ascii")):
        raise MutationError(HTTPStatus.FORBIDDEN, "forbidden")
    if not valid_host(headers, urlsplit(origin).netloc):
        raise MutationError(HTTPStatus.FORBIDDEN, "forbidden")
    if "Origin" in headers and _header(headers, "Origin") != origin:
        raise MutationError(HTTPStatus.FORBIDDEN, "forbidden")
    content_type = _header(headers, "Content-Type") or ""
    if content_type.split(";", 1)[0].strip().lower() != "application/json" or "Transfer-Encoding" in headers:
        raise MutationError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "expected_application_json")
    length = _header(headers, "Content-Length") or ""
    if not length or not length.isascii() or not length.isdecimal():
        raise MutationError(HTTPStatus.LENGTH_REQUIRED, "content_length_required")
    if len(length) > 5 or int(length) > MAX_BODY:
        raise MutationError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
    return int(length)


def _unique_fields(pairs: list[tuple]) -> dict:
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate field")
    return fields


def _valid_text(value: object, limit: int, *, multiline: bool) -> bool:
    if not isinstance(value, str) or len(value) > limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    allowed = "\n\r\t" if multiline else ""
    return all(ord(ch) >= 32 and ord(ch) != 127 or ch in allowed for ch in value)


def _read_payload(path: str, reader: BinaryIO, length: int) -> dict[str, str]:
    raw = reader.read(length)
    if path not in _FIELDS:
        raise MutationError(HTTPStatus.NOT_FOUND, "not_found")
    required, defaults = _FIELDS[path]
    try:
        if len(raw) != length:
            raise ValueError("truncated body")
        fields = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (ValueError, RecursionError):
        raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_json") from None
    if not isinstance(fields, dict) or not required <= fields.keys() <= required | defaults.keys():
        raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_fields")
    for key, value in fields.items():
        if not _valid_text(value, _LIMITS[key], multiline=key in {"note", "text"}):
            raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_fields")
        if key in required and not value.strip():
            raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_fields")
    return fields


def _require_profile(profile: str, db_path: Path) -> None:
    try:
        value = store.load_profile(profile, user_dir=db_path.parent / "profiles")
    except (OSError, ValueError, yaml.YAMLError):
        value = None
    if not isinstance(value, dict) or not value:
        raise MutationError(HTTPStatus.BAD_REQUEST, "profile_not_found")


def _set_state(fields: dict[str, str], db_path: Path) -> dict:
    if fields["state"] not in review.STATES:
        raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_state")
    key = review.set_state(fields["identity_key"], fields["state"], db_path=db_path)
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        state, changed = conn.execute("SELECT review_state, review_changed_at FROM jobs WHERE identity_key = ?",
                                      (key,)).fetchone()
        counts = dict.fromkeys(review.STATES, 0)
        counts.update(conn.execute("SELECT review_state, COUNT(*) FROM jobs GROUP BY review_state"))
    return {"job": {"identity_key": sanitize.clean_text(key), "review_state": state, "review_changed_at": changed},
            "state_counts": counts}


def _add_note(fields: dict[str, str], db_path: Path) -> dict:
    note = sanitize.clean_multiline_text(fields["text"])
    if not note:
        raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_note")
    key = review.add_note(fields["identity_key"], note, db_path=db_path)
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        current = conn.execute("SELECT notes FROM jobs WHERE identity_key = ?", (key,)).fetchone()[0]
    return {"identity_key": sanitize.clean_text(key), "notes": sanitize.clean_multiline_text(current)}


def _feedback(fields: dict[str, str], db_path: Path) -> dict:
    profile = fields.get("profile") or None
    if profile:
        _require_profile(profile, db_path)
    note = sanitize.clean_multiline_text(fields["note"]) if "note" in fields else None
    key = review.set_feedback(fields["identity_key"], fields["relevance"], fields.get("reason_code"),
                              note, profile=profile, db_path=db_path)
    with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as conn:
        if profile:
            row = conn.execute("SELECT relevance, reason_code, note FROM profile_job_feedback"
                               " WHERE identity_key = ? AND profile_slug = ?", (key, profile)).fetchone()
        else:
            row = conn.execute("SELECT relevance, reason_code, note FROM job_feedback WHERE identity_key = ?",
                               (key,)).fetchone()
    relevance, reason, current_note = row
    return {"identity_key": sanitize.clean_text(key), "profile": profile, "relevance": relevance,
            "reason_code": reason, "note": sanitize.clean_multiline_text(current_note)}


def _watch_change(fields: dict[str, str], db_path: Path) -> dict:
    profile = fields["profile"]
    _require_profile(profile, db_path)
    remove = "seed_key" in fields
    raw = fields.get("seed_key") if remove else fields.get("board")
    if "://" in raw and (remove or sanitize.safe_http_url(raw) is None):
        raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_board")
    if not (result := resolve_board_key(raw)):
        raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_board")
    seed_key, company = result
    if sanitize.clean_text(seed_key) != seed_key:
        raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_board")
    with closing(ledger.connect(db_path)) as conn:
        if remove:
            watchlist.remove(conn, profile, seed_key)
            return {"removed": seed_key}
        watchlist.add(conn, profile, seed_key, company)
        entry = next(item for item in watchlist.entries(conn, profile) if item["seed_key"] == seed_key)
    return {"profile_slug": profile, **{key: sanitize.clean_text(value) for key, value in entry.items()}}


def apply_mutation(path: str, reader: BinaryIO, length: int, db_path: Path) -> dict:
    """Decode a validated request, run the edit and return a sanitized result."""
    fields = _read_payload(path, reader, length)
    actions = {"/api/jobs/state": _set_state, "/api/jobs/notes": _add_note, "/api/jobs/feedback": _feedback,
               "/api/watchlist/add": _watch_change, "/api/watchlist/remove": _watch_change}
    try:
        return actions[path](fields, db_path)
    except (LookupError, ValueError) as exc:
        if isinstance(exc, MutationError):
            raise
        if isinstance(exc, LookupError):
            raise MutationError(HTTPStatus.NOT_FOUND, "job_not_found") from None
        raise MutationError(HTTPStatus.BAD_REQUEST, "invalid_fields") from None
