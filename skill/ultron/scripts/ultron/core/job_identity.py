"""Stable, conservative identity for job-posting URLs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_TRACKING_KEYS = frozenset(
    {
        "campaign",
        "fbclid",
        "gclid",
        "gh_src",
        "lever-source",
        "ref",
        "referer",
        "referrer",
        "source",
        "trk",
        "tracking",
    }
)
_JOB_ID_QUERY_KEY_PRIORITY = (
    "gh_jid",
    "jobid",
    "job_id",
    "jid",
    "rid",
    "wdi",
    "positionid",
    "requisitionid",
    "job",
    "id",
)
_JOB_ID_QUERY_KEYS = frozenset(_JOB_ID_QUERY_KEY_PRIORITY)


@dataclass(frozen=True, slots=True)
class CanonicalJobIdentity:
    identity_key: str
    canonical_url: str
    fallback_reason: str | None = None


def _is_tracking_key(key: str) -> bool:
    low = key.casefold()
    return low.startswith("utm_") or low in _TRACKING_KEYS


def _source_vendor(source_type: str, source_key: str) -> tuple[str, str]:
    source_type_low = str(source_type or "").strip().casefold()
    source_key_low = str(source_key or "").strip().casefold()
    for vendor in ("greenhouse", "lever", "ashby", "workable", "workday", "personio"):
        if source_key_low.startswith(f"{vendor}:"):
            return vendor, source_key_low.split(":", 1)[1].strip()
        if source_type_low == vendor:
            return vendor, source_key_low
    return "", ""


def _fallback(raw: str, reason: str) -> CanonicalJobIdentity:
    value = raw.strip().casefold()
    logger.warning("job_identity_fallback reason=%s url=%r", reason, raw[:240])
    return CanonicalJobIdentity(identity_key=f"raw:{value}", canonical_url=value, fallback_reason=reason)


def _normalized_host(parsed) -> tuple[str, str]:
    host = parsed.hostname
    if not host:
        raise ValueError("missing_host")
    ascii_host = host.rstrip(".").encode("idna").decode("ascii").casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_port") from exc
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    rendered_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    netloc = rendered_host if port is None or default_port else f"{rendered_host}:{port}"
    return ascii_host, netloc


def _query_pairs(query: str) -> list[tuple[str, str]]:
    pairs = [(key, value) for key, value in parse_qsl(query, keep_blank_values=True) if not _is_tracking_key(key)]
    return sorted(pairs, key=lambda item: (item[0].casefold(), item[1]))


def _query_job_id(pairs: list[tuple[str, str]]) -> str:
    values: dict[str, str] = {}
    for key, value in pairs:
        folded = key.casefold()
        if folded in _JOB_ID_QUERY_KEYS and value.strip() and folded not in values:
            values[folded] = value.strip().casefold()
    for key in _JOB_ID_QUERY_KEY_PRIORITY:
        if key in values:
            return values[key]
    return ""


def _host_matches(host: str, domain: str) -> bool:
    normalized = str(host or "").casefold().rstrip(".")
    suffix = str(domain or "").casefold().lstrip(".")
    return normalized == suffix or normalized.endswith(f".{suffix}")


def _vendor_identity(*, host: str, path: str, pairs: list[tuple[str, str]], source_type: str, source_key: str) -> str:
    vendor, source_board = _source_vendor(source_type, source_key)
    low_path = path.casefold()
    query_id = _query_job_id(pairs)

    if vendor == "greenhouse" or _host_matches(host, "greenhouse.io"):
        path_match = re.search(r"/(?:embed/)?jobs?/([0-9]+)(?:/|$)", low_path)
        job_id = (path_match.group(1) if path_match else "") or query_id
        board_match = re.match(r"/([^/]+)/(?:embed/)?jobs?", low_path)
        board = source_board or (board_match.group(1) if board_match else "") or host
        if job_id:
            return f"greenhouse:{board}:{job_id}"

    if vendor == "lever" or _host_matches(host, "lever.co"):
        match = re.match(r"/([^/]+)/([0-9a-f-]{16,})(?:/|$)", low_path)
        if match:
            return f"lever:{match.group(1)}:{match.group(2)}"

    if vendor == "ashby" or _host_matches(host, "ashbyhq.com"):
        match = re.match(r"/([^/]+)/([0-9a-f-]{16,})(?:/|$)", low_path)
        if match:
            return f"ashby:{match.group(1)}:{match.group(2)}"

    if vendor == "workable" or _host_matches(host, "workable.com"):
        match = re.match(r"/([^/]+)/j/([a-z0-9]{6,})(?:/|$)", low_path)
        if match:
            return f"workable:{match.group(1)}:{match.group(2)}"
        short = re.match(r"/j/([a-z0-9]{6,})(?:/|$)", low_path)
        if short:
            board = source_board or host
            return f"workable:{board}:{short.group(1)}"

    if vendor == "workday" or _host_matches(host, "myworkdayjobs.com"):
        requisitions = re.findall(r"_([a-z]{1,3}-?[0-9]+)(?:/|$)", low_path)
        job_id = (requisitions[-1] if requisitions else "") or query_id
        if job_id:
            return f"workday:{host}:{job_id}"

    if vendor == "personio" or _host_matches(host, "personio.com") or _host_matches(host, "personio.de"):
        match = re.search(r"/(?:job|position)/([a-z0-9-]+)(?:/|$)", low_path)
        job_id = (match.group(1) if match else "") or query_id
        if job_id:
            tenant = source_board or host.split(".", 1)[0]
            return f"personio:{tenant}:{job_id}"

    return ""


def canonical_job_identity(url: str, *, source_type: str = "", source_key: str = "") -> CanonicalJobIdentity:
    """Return a canonical URL and stable identity without dropping unknown ID parameters."""

    raw = str(url or "").strip()
    if not raw:
        return _fallback(raw, "empty_url")
    candidate = raw if re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.IGNORECASE) else f"https://{raw.lstrip('/')}"
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            return _fallback(raw, "unsupported_scheme")
        host, netloc = _normalized_host(parsed)
        path = parsed.path or ""
        if path != "/":
            path = path.rstrip("/")
        pairs = _query_pairs(parsed.query)
        query = urlencode(pairs, doseq=True)
        canonical_url = urlunsplit((scheme, netloc, path, query, ""))
        vendor_key = _vendor_identity(
            host=host,
            path=path,
            pairs=pairs,
            source_type=source_type,
            source_key=source_key,
        )
        url_key = urlunsplit(("", netloc, path, query, ""))
        return CanonicalJobIdentity(identity_key=vendor_key or f"url:{url_key}", canonical_url=canonical_url)
    except (UnicodeError, ValueError) as exc:
        return _fallback(raw, str(exc) or type(exc).__name__)


__all__ = ["CanonicalJobIdentity", "canonical_job_identity"]
