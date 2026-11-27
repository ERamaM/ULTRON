"""Bounded HTTP reads for an explicitly configured IPv4 loopback service."""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from ultron.infra.net import run_bounded
from ultron.infra.urlpolicy import URLRejected

_LOOPBACK_SLOTS = threading.BoundedSemaphore(8)


@dataclass(frozen=True, slots=True)
class ExplicitLoopbackOrigin:
    """The validated host and explicit port of a local HTTP service."""

    host: str
    port: int


def _parts(url: str):
    # Reject URL ambiguities before either parser can normalize them.
    if len(url) > 8192 or "#" in url or "\\" in url or any(char.isspace() or ord(char) < 32 for char in url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}
                and parts.port and parts.netloc.lower() == f"{parts.hostname}:{parts.port}"):
            return parts
    except ValueError:  # malformed authority or port
        return None
    return None


def explicit_loopback_origin(url: str) -> ExplicitLoopbackOrigin | None:
    """Recognize an unambiguous HTTP origin on localhost or 127.0.0.1."""
    parts = _parts(url)
    if parts and parts.path in {"", "/"} and "?" not in url:
        return ExplicitLoopbackOrigin(parts.hostname, parts.port)
    return None


def read_loopback(
    origin: ExplicitLoopbackOrigin, url: str, *, timeout: float, max_bytes: int = 2_000_000,
    deadline: float | None = None,
) -> tuple[int, bytes]:
    """Read a same-origin response within the time budget, without proxies, DNS or redirects."""
    stop = time.monotonic() + timeout
    if deadline is not None:
        stop = min(stop, deadline)
    parts = _parts(url)
    if not parts or (parts.hostname, parts.port) != (origin.host, origin.port):
        raise URLRejected("loopback_origin_mismatch")
    target = urlunsplit(parts._replace(netloc=f"127.0.0.1:{origin.port}"))
    headers = {"Host": parts.netloc, "Accept-Encoding": "identity"}

    def timeout_error() -> httpx.ReadTimeout:
        return httpx.ReadTimeout("Loopback request exceeded its time budget")

    def remaining() -> float:
        seconds = stop - time.monotonic()
        if seconds <= 0:
            raise timeout_error()
        return seconds

    return run_bounded(
        lambda: _read(target, headers, max_bytes, remaining),
        slots=_LOOPBACK_SLOTS, remaining=remaining, timeout_error=timeout_error,
    )


def _read(
    target: str, headers: dict[str, str], max_bytes: int, remaining: Callable[[], float],
) -> tuple[int, bytes]:
    with (
        httpx.Client(trust_env=False, follow_redirects=False) as client,
        client.stream("GET", target, headers=headers, timeout=remaining()) as response,
    ):
        remaining()
        if 300 <= response.status_code < 400:
            raise URLRejected("redirect_rejected")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise URLRejected("loopback_content_encoding")
        body = bytearray()
        for chunk in response.iter_raw():
            remaining()
            if len(body) + len(chunk) > max_bytes:
                raise URLRejected("response_too_large")
            body.extend(chunk)
        remaining()
        return response.status_code, bytes(body)
