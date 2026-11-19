"""HTTPX transport that connects only to addresses the caller already validated.

``HttpClient`` resolves and validates a hostname once per hop and passes the answer in the request extension
``PINNED_ADDRESSES``. This transport sends the request to those numeric addresses, so the hostname is never resolved
again at connect time. The ``Host`` header and TLS (SNI and certificate hostname verification) keep the original
hostname. Each hostname gets its own connection pool, so a connection authenticated for one hostname is never reused
for another hostname that shares an address.
"""

from __future__ import annotations

import ipaddress
import ssl
from collections import OrderedDict
from collections.abc import Callable
from functools import lru_cache

import httpx

from ultron.core.urlpolicy import URLRejected

PINNED_ADDRESSES = "ultron.pinned_addresses"
MAX_PINNED_ATTEMPTS = 3  # bounds connect fallbacks so many unreachable addresses cannot stall a run
MAX_POOLS = 16  # least recently used per-hostname pools beyond this are closed

WireFactory = Callable[[], httpx.BaseTransport]


@lru_cache(maxsize=1)
def _shared_ssl_context() -> ssl.SSLContext:
    """One verified TLS context per process (loading the CA bundle is slow); verification stays required."""
    return httpx.create_ssl_context(verify=True, trust_env=False)


def default_wire() -> httpx.BaseTransport:
    return httpx.HTTPTransport(verify=_shared_ssl_context(), trust_env=False, retries=0)


class PinnedTransport(httpx.BaseTransport):
    def __init__(self, wire_factory: WireFactory = default_wire, max_pools: int = MAX_POOLS) -> None:
        self._wire_factory = wire_factory
        self._max_pools = max(1, max_pools)
        self._wires: OrderedDict[str, httpx.BaseTransport] = OrderedDict()

    def _wire(self, hostname: str) -> httpx.BaseTransport:
        wire = self._wires.get(hostname)
        if wire is not None:
            self._wires.move_to_end(hostname)
            return wire
        # HttpClient reads and closes each response before the next request, so evicting an idle pool is safe and
        # keeps open keep-alive sockets bounded on runs that touch hundreds of hosts.
        while len(self._wires) >= self._max_pools:
            _, evicted = self._wires.popitem(last=False)
            evicted.close()
        wire = self._wires[hostname] = self._wire_factory()
        return wire

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        addresses = tuple(request.extensions.get(PINNED_ADDRESSES) or ())
        if not addresses:
            raise URLRejected("unpinned_request")  # fail closed: never let the OS resolve the name here
        hostname = request.url.raw_host.decode("ascii")  # IDNA-encoded, as TLS SNI requires
        wire = self._wire(hostname)
        ordered = sorted(addresses, key=lambda value: ipaddress.ip_address(value).version)[:MAX_PINNED_ATTEMPTS]
        for index, address in enumerate(ordered):
            headers = request.headers.copy()
            headers["Host"] = request.headers.get("Host") or request.url.netloc.decode("ascii")
            pinned = httpx.Request(
                request.method,
                request.url.copy_with(host=address),
                headers=headers,
                stream=request.stream,
                extensions={**request.extensions, "sni_hostname": hostname},
            )
            try:
                return wire.handle_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(ordered) - 1:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    def close(self) -> None:
        wires, self._wires = self._wires, OrderedDict()
        for wire in wires.values():
            wire.close()


__all__ = ["MAX_PINNED_ATTEMPTS", "MAX_POOLS", "PINNED_ADDRESSES", "PinnedTransport", "default_wire"]
