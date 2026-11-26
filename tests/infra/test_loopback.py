import httpx
import pytest

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
