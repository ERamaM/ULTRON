"""Network admission policy for URLs that originate outside the application.

The discovery pipeline consumes URLs returned by search engines.  Treat those
values as untrusted until both their textual form and their resolved addresses
have passed this module.  Fetchers re-run the policy for every redirect so a
public hostname cannot redirect into a local network.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

Resolver = Callable[[str, int], Iterable[str]]
DEFAULT_ALLOWED_PORTS = frozenset({80, 443})


class URLRejected(ValueError):
    """Raised when an untrusted URL is not safe to request."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AdmittedURL:
    """Validated public HTTP(S) URL with extracted hostname and port."""
    url: str
    hostname: str
    port: int


def _default_resolver(hostname: str, port: int) -> tuple[str, ...]:
    rows = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return tuple(dict.fromkeys(str(row[4][0]).split("%", 1)[0] for row in rows))


def _normalized_domains(domains: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(value for item in domains if (value := str(item or "").strip().lower().lstrip(".").rstrip(".")))
    )


def domain_is_allowed(hostname: str, allowed_domains: Iterable[str]) -> bool:
    """Return True if ``hostname`` matches ``allowed_domains`` or the list is empty."""
    allowed = _normalized_domains(allowed_domains)
    if not allowed:
        return True
    host = hostname.lower().rstrip(".")
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed)


def _validate_address(value: str) -> None:
    if "%" in value:
        raise URLRejected("invalid_address")
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError as exc:
        raise URLRejected("invalid_address") from exc
    if (
        not address.is_global
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or (isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None)
    ):
        raise URLRejected("non_public_address")


def resolve_public_addresses(hostname: str, port: int, *, resolver: Resolver | None = None) -> tuple[str, ...]:
    """Return the complete validated answer set used for numeric connections."""
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        addresses = (str(literal),)
    else:
        try:
            addresses = tuple((resolver or _default_resolver)(hostname, port))
        except OSError as exc:
            raise URLRejected("dns_resolution_failed") from exc
    if not addresses:
        raise URLRejected("dns_resolution_failed")
    if len(addresses) > 16:
        raise URLRejected("dns_answer_limit")
    for address in addresses:
        _validate_address(address)
    return tuple(dict.fromkeys(str(ipaddress.ip_address(value)) for value in addresses))


def safe_url_label(value: str) -> str:
    """Return a log-safe origin without credentials, path, query or fragment."""

    try:
        parts = urlsplit(str(value or ""))
        host = parts.hostname or ""
        if not host:
            return "<invalid-url>"
        try:
            port = parts.port
        except ValueError:
            return f"{parts.scheme or 'unknown'}://{host}:<invalid-port>"
        netloc = f"[{host}]" if ":" in host else host
        if port is not None and not (
            (parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)
        ):
            netloc = f"{netloc}:{port}"
        return urlunsplit((parts.scheme.lower(), netloc, "", "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


def admit_url(
    value: str,
    *,
    allowed_domains: Iterable[str] = (),
    allowed_ports: Iterable[int] = DEFAULT_ALLOWED_PORTS,
    resolve_dns: bool = False,
    resolver: Resolver | None = None,
) -> AdmittedURL:
    """Validate an HTTP(S) URL and optionally all addresses returned by DNS."""

    raw = str(value or "").strip()
    try:
        parts = urlsplit(raw)
    except ValueError as exc:
        raise URLRejected("invalid_url") from exc
    if parts.scheme.lower() not in {"http", "https"}:
        raise URLRejected("invalid_scheme")
    if parts.username is not None or parts.password is not None:
        raise URLRejected("credentials_in_url")
    hostname = (parts.hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise URLRejected("missing_hostname")
    try:
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise URLRejected("invalid_port") from exc
    if port not in {int(item) for item in allowed_ports}:
        raise URLRejected("port_not_allowed")
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        _validate_address(str(literal))
    if not domain_is_allowed(hostname, allowed_domains):
        raise URLRejected("domain_not_allowed")
    if literal is None and resolve_dns:
        try:
            addresses = tuple((resolver or _default_resolver)(hostname, port))
        except (OSError, socket.gaierror) as exc:
            raise URLRejected("dns_resolution_failed") from exc
        if not addresses:
            raise URLRejected("dns_resolution_failed")
        for address in addresses:
            _validate_address(address)

    # Strip a fragment because it is never sent to the server.  Preserve the
    # path/query because ATS identifiers often live there.
    normalized = urlunsplit((parts.scheme.lower(), parts.netloc, parts.path, parts.query, ""))
    return AdmittedURL(normalized, hostname, port)


__all__ = [
    "DEFAULT_ALLOWED_PORTS",
    "AdmittedURL",
    "URLRejected",
    "admit_url",
    "domain_is_allowed",
    "resolve_public_addresses",
    "safe_url_label",
]
