"""Connections go only to the addresses validated for the hostname; TLS still authenticates the hostname."""

from __future__ import annotations

import ssl

import httpcore
import httpx
import pytest
from httpcore import MockStream, SyncBackend

from ultron.infra.net import HttpClient, URLRejected
from ultron.infra.pinned_transport import PINNED_ADDRESSES, PinnedTransport

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "93.184.216.35"


class Wire:
    """Records what the real httpx/httpcore stack would put on the network, without any socket."""

    def __init__(self, responses: dict[str, bytes], refuse: frozenset[str] = frozenset()) -> None:
        self.responses = responses
        self.refuse = refuse
        self.connects: list[tuple[str, int]] = []
        self.tls: list[tuple[str | None, ssl.SSLContext]] = []
        self.sent = bytearray()

    def install(self, monkeypatch) -> None:
        wire = self

        class Stream(MockStream):
            def write(self, buffer: bytes, timeout: float | None = None) -> None:
                wire.sent.extend(buffer)

            def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                wire.tls.append((server_hostname, ssl_context))
                return self

        def connect_tcp(backend, host, port, timeout=None, local_address=None, socket_options=None):
            wire.connects.append((host, port))
            if host in wire.refuse:
                raise httpcore.ConnectError("refused")
            return Stream([wire.responses.get(host, b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")])

        monkeypatch.setattr(SyncBackend, "connect_tcp", connect_tcp)


OK = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"


def _client(resolver) -> HttpClient:
    return HttpClient(max_requests=10, max_requests_per_host=10, min_interval_per_host=0.0, resolver=resolver,
                      sleep=lambda s: None)


def test_connection_pinned_to_validated_address_despite_rebinding(monkeypatch):
    wire = Wire({PUBLIC_A: OK})
    wire.install(monkeypatch)
    answers = iter([[PUBLIC_A], ["127.0.0.1"]])  # a second lookup would return loopback
    with _client(lambda host, port: next(answers)) as client:
        assert client.get_text("https://jobs.example.org/a")[0] == 200
    assert wire.connects == [(PUBLIC_A, 443)]
    server_hostname, context = wire.tls[0]
    assert server_hostname == "jobs.example.org"
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert b"Host: jobs.example.org\r\n" in bytes(wire.sent)
    assert b"GET /a HTTP/1.1" in bytes(wire.sent)


def test_falls_back_to_next_validated_address(monkeypatch):
    wire = Wire({PUBLIC_B: OK}, refuse=frozenset({PUBLIC_A}))
    wire.install(monkeypatch)
    with _client(lambda host, port: [PUBLIC_A, PUBLIC_B]) as client:
        assert client.get_text("https://jobs.example.org/")[0] == 200
    assert wire.connects == [(PUBLIC_A, 443), (PUBLIC_B, 443)]


def test_every_redirect_hop_is_revalidated_and_pinned(monkeypatch):
    redirect = b"HTTP/1.1 302 Found\r\nLocation: https://cdn.example.net/b\r\nContent-Length: 0\r\n\r\n"
    wire = Wire({PUBLIC_A: redirect, PUBLIC_B: OK})
    wire.install(monkeypatch)
    hosts = {"jobs.example.org": [PUBLIC_A], "cdn.example.net": [PUBLIC_B]}
    with _client(lambda host, port: hosts[host]) as client:
        status, final_url, _ = client.get_text("https://jobs.example.org/a")
    assert (status, final_url) == (200, "https://cdn.example.net/b")
    assert wire.connects == [(PUBLIC_A, 443), (PUBLIC_B, 443)]
    assert [name for name, _ in wire.tls] == ["jobs.example.org", "cdn.example.net"]


def test_redirect_to_private_address_never_connects(monkeypatch):
    redirect = b"HTTP/1.1 302 Found\r\nLocation: https://internal.example.net/\r\nContent-Length: 0\r\n\r\n"
    wire = Wire({PUBLIC_A: redirect})
    wire.install(monkeypatch)
    hosts = {"jobs.example.org": [PUBLIC_A], "internal.example.net": ["169.254.169.254"]}
    with _client(lambda host, port: hosts[host]) as client, pytest.raises(URLRejected):
        client.get_text("https://jobs.example.org/")
    assert wire.connects == [(PUBLIC_A, 443)]


def test_plain_http_is_pinned_too(monkeypatch):
    wire = Wire({PUBLIC_A: OK})
    wire.install(monkeypatch)
    with _client(lambda host, port: [PUBLIC_A]) as client:
        assert client.get_text("http://jobs.example.org/")[0] == 200
    assert wire.connects == [(PUBLIC_A, 80)] and wire.tls == []
    assert b"Host: jobs.example.org\r\n" in bytes(wire.sent)


def test_per_hostname_pools_are_bounded_and_evicted_pools_closed():
    class Recording(httpx.MockTransport):
        closed = 0

        def close(self) -> None:
            Recording.closed += 1

    transport = PinnedTransport(wire_factory=lambda: Recording(lambda r: httpx.Response(200)), max_pools=4)
    for i in range(10):
        request = httpx.Request("GET", f"https://h{i}.example.org/", extensions={PINNED_ADDRESSES: (PUBLIC_A,)})
        assert transport.handle_request(request).status_code == 200
    assert len(transport._wires) == 4 and Recording.closed == 6
    assert list(transport._wires) == [f"h{i}.example.org" for i in range(6, 10)]
    transport.close()
    assert Recording.closed == 10 and not transport._wires


def test_unpinned_request_fails_closed():
    transport = PinnedTransport(wire_factory=lambda: httpx.MockTransport(lambda r: httpx.Response(200)))
    with pytest.raises(URLRejected):
        transport.handle_request(httpx.Request("GET", "https://jobs.example.org/"))
    request = httpx.Request("GET", "https://jobs.example.org/", extensions={PINNED_ADDRESSES: ()})
    with pytest.raises(URLRejected):
        transport.handle_request(request)
