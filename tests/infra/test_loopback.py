import threading

import httpx
import pytest

from ultron.infra import loopback
from ultron.infra.loopback import explicit_loopback_origin, read_loopback
from ultron.infra.urlpolicy import URLRejected, admit_url


@pytest.mark.parametrize("url", [
    "http://127.0.0.1", "http://0.0.0.0:8888", "http://198.51.100.10:8888",
    "https://localhost:8888", "http://localhost:8888/path", "http://localhost:8888/#",
    "http://localhost:8888/?", "http://user@localhost:8888", "http://127.0.1:8888",
    "http://[::1]:8888", "http://localhost:99999", " http://localhost:8888",
    "http://localhost:0", "http://localhost:8888\\@other.test",
])
def test_disallowed_origins(url):
    assert explicit_loopback_origin(url) is None


@pytest.mark.parametrize(("status", "body", "encoding", "error"), [
    (200, b"ok", "identity", None),
    (301, b"", "identity", "redirect_rejected"),
    (302, b"", "identity", "redirect_rejected"),
    (200, b"large", "identity", "response_too_large"),
    (200, b"", "gzip", "loopback_content_encoding"),
])
def test_loopback_stream_isolated(monkeypatch, status, body, encoding, error):
    origin = explicit_loopback_origin("http://localhost:8888/")
    assert explicit_loopback_origin("http://127.0.0.1:8888")
    real_client = httpx.Client

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield body

    def respond(request):
        assert str(request.url) == "http://127.0.0.1:8888/search?q=hello"
        assert request.headers["host"] == "localhost:8888"
        return httpx.Response(status, stream=Stream(),
                              headers={"Location": "https://example.org/", "Content-Encoding": encoding})

    def client(**options):
        assert options["trust_env"] is options["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(respond), **options)

    monkeypatch.setattr(httpx, "Client", client)
    monkeypatch.setenv("HTTP_PROXY", "http://example.org:9000")
    with pytest.raises(URLRejected):
        admit_url("http://127.0.0.1:8888")
    with pytest.raises(URLRejected, match="loopback_origin_mismatch"):
        read_loopback(origin, "http://127.0.0.1:8888/search", timeout=1)
    url = "http://localhost:8888/search?q=hello"
    if error:
        with pytest.raises(URLRejected, match=error):
            read_loopback(origin, url, timeout=1, max_bytes=2)
    else:
        assert read_loopback(origin, url, timeout=1, max_bytes=2) == (200, b"ok")


@pytest.mark.parametrize("late_at", ["headers", "chunk", "eof"])
def test_late_loopback_response_is_rejected(monkeypatch, late_at):
    now = [0.0]
    real_client = httpx.Client

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            if late_at == "chunk":
                now[0] = 2.0
                yield b"ok"
            if late_at == "eof":
                now[0] = 2.0

    def respond(_request):
        if late_at == "headers":
            now[0] = 2.0
        return httpx.Response(200, stream=Stream())

    monkeypatch.setattr(loopback, "time", type("Clock", (), {"monotonic": lambda: now[0]}))
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    url = "http://localhost:8888"
    with pytest.raises(httpx.ReadTimeout):
        read_loopback(explicit_loopback_origin(url), url + "/", timeout=1)


@pytest.mark.parametrize("blocked_at", ["headers", "body"])
def test_blocking_loopback_read_returns_before_release(monkeypatch, blocked_at):
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    errors = []
    real_client = httpx.Client

    def wait():
        entered.set()
        release.wait(timeout=8)

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            if blocked_at == "body":
                wait()
            yield b"ok"

    def respond(_request):
        if blocked_at == "headers":
            wait()
        return httpx.Response(200, stream=Stream())

    def fetch():
        url = "http://localhost:8888"
        try:
            read_loopback(explicit_loopback_origin(url), url + "/", timeout=0.25)
        except httpx.ReadTimeout as error:
            errors.append(error)
        finally:
            returned.set()

    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    caller = threading.Thread(target=fetch, daemon=True)
    caller.start()
    try:
        assert entered.wait(timeout=4)
        assert returned.wait(timeout=2), "blocked read outlasted the request budget"
        assert len(errors) == 1
    finally:
        release.set()
        caller.join(timeout=4)
