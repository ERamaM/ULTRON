"""Bounded HTTP reads for an explicitly configured IPv4 loopback service."""

import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx

from ultron.infra.urlpolicy import URLRejected


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
) -> tuple[int, bytes]:
    """Read a same-origin response without proxies, DNS lookups or redirects."""
    parts = _parts(url)
    if not parts or (parts.hostname, parts.port) != (origin.host, origin.port):
        raise URLRejected("loopback_origin_mismatch")
    target = urlunsplit(parts._replace(netloc=f"127.0.0.1:{origin.port}"))
    headers = {"Host": parts.netloc, "Accept-Encoding": "identity"}
    deadline = time.monotonic() + timeout
    with (
        httpx.Client(trust_env=False, follow_redirects=False, timeout=timeout) as client,
        client.stream("GET", target, headers=headers) as response,
    ):
        if 300 <= response.status_code < 400:
            raise URLRejected("redirect_rejected")
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise URLRejected("loopback_content_encoding")
        body = bytearray()
        for chunk in response.iter_raw():
            if time.monotonic() >= deadline:
                raise httpx.ReadTimeout("Loopback request exceeded its time budget")
            if len(body) + len(chunk) > max_bytes:
                raise URLRejected("response_too_large")
            body.extend(chunk)
        return response.status_code, bytes(body)
