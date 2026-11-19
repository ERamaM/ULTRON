from __future__ import annotations

import httpx
import pytest

from ultron import net
from ultron.core import urlpolicy
from ultron.net import HttpClient, HttpError, RequestBudgetExceeded, URLRejected


def public_resolver(hostname: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def make_client(handler, **kwargs) -> HttpClient:
    options = {
        "max_requests": 50,
        "max_requests_per_host": 50,
        "min_interval_per_host": 0.0,
        "transport": httpx.MockTransport(handler),
        "resolver": public_resolver,
        "sleep": lambda seconds: None,
    }
    options.update(kwargs)
    return HttpClient(**options)


def test_urlrejected_is_policy_class():
    assert net.URLRejected is urlpolicy.URLRejected


def test_rejects_private_ip():
    client = make_client(lambda r: httpx.Response(200, text="x"), resolver=lambda h, p: ["10.0.0.5"])
    with pytest.raises(URLRejected):
        client.get_text("https://x.test/")
    assert client.requests_made == 0


def test_rejects_redirect_to_localhost():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/"})

    client = make_client(handler)
    with pytest.raises(URLRejected):
        client.get_text("https://x.test/")


def test_follows_public_redirect_and_reports_final_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(301, headers={"Location": "/b"})
        return httpx.Response(200, text="ok")

    status, final_url, text = make_client(handler).get_text("https://x.test/a")
    assert (status, final_url, text) == (200, "https://x.test/b", "ok")


def test_too_many_redirects():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/loop"})

    with pytest.raises(HttpError):
        make_client(handler).get_text("https://x.test/loop")


def test_retries_429_then_succeeds():
    calls = []
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(429) if len(calls) < 3 else httpx.Response(200, text="done")

    client = make_client(handler, sleep=waits.append)
    status, _, text = client.get_text("https://x.test/")
    assert (status, text) == (200, "done")
    assert client.requests_made == 3
    assert waits == [0.5, 1.0]


def test_retry_after_respected_when_small():
    calls = []
    waits: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(503, headers={"Retry-After": "3"})
        return httpx.Response(200, text="ok")

    make_client(handler, sleep=waits.append).get_text("https://x.test/")
    assert waits == [3.0]


def test_budget_exceeded():
    client = make_client(lambda r: httpx.Response(200, text="x"), max_requests=2)
    client.get_text("https://x.test/1")
    client.get_text("https://x.test/2")
    with pytest.raises(RequestBudgetExceeded):
        client.get_text("https://x.test/3")


def test_per_host_budget_exceeded():
    client = make_client(lambda r: httpx.Response(200, text="x"), max_requests_per_host=1)
    client.get_text("https://a.test/")
    client.get_text("https://b.test/")
    with pytest.raises(RequestBudgetExceeded):
        client.get_text("https://a.test/again")


def test_truncates_at_max_bytes():
    client = make_client(lambda r: httpx.Response(200, content=b"a" * 10_000), max_bytes=1000)
    _, _, text = client.get_text("https://x.test/")
    assert len(text) <= 1000


def test_404_raises_http_error_with_status():
    client = make_client(lambda r: httpx.Response(404, text="nope"))
    with pytest.raises(HttpError) as info:
        client.request_text("https://x.test/")
    assert info.value.status_code == 404


def test_request_json_get_and_post():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["body"] = request.content
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    assert client.request_json("https://x.test/api", timeout=5) == {"ok": True}
    assert seen["method"] == "GET"
    from ultron import __version__

    assert seen["ua"] == f"ULTRON/{__version__}"  # R4: from the package version, no placeholder URL
    assert client.request_json("https://x.test/api", method="POST", timeout=5, json_body={"q": 1}) == {"ok": True}
    assert seen["method"] == "POST"
    assert b'"q"' in seen["body"]


def test_request_json_invalid_json_raises_http_error():
    client = make_client(lambda r: httpx.Response(200, text="<html>"))
    with pytest.raises(HttpError):
        client.request_json("https://x.test/")


def test_min_interval_per_host_waits():
    waits: list[float] = []
    now = [100.0]
    client = make_client(
        lambda r: httpx.Response(200, text="x"),
        min_interval_per_host=0.5,
        sleep=waits.append,
        clock=lambda: now[0],
    )
    client.get_text("https://x.test/1")
    client.get_text("https://x.test/2")
    assert waits == [0.5]


def test_context_manager():
    with make_client(lambda r: httpx.Response(200, text="x")) as client:
        assert client.get_text("https://x.test/")[0] == 200


def test_ignores_proxy_env(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    client = make_client(lambda r: httpx.Response(200, text="x"))
    assert client._client.trust_env is False
    assert client.get_text("https://x.test/")[0] == 200


def test_bogus_charset_falls_back_to_utf8():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content="café".encode(), headers={"Content-Type": "text/html; charset=foo"})

    _, _, text = make_client(handler).get_text("https://x.test/")
    assert text == "café"


def test_redirect_without_location_is_error_for_request_helpers():
    client = make_client(lambda r: httpx.Response(302))
    assert client.get_text("https://x.test/")[0] == 302
    with pytest.raises(HttpError) as info:
        client.request_text("https://x.test/")
    assert info.value.status_code == 302
    with pytest.raises(HttpError) as info:
        client.request_json("https://x.test/")
    assert info.value.status_code == 302


def test_redirect_hops_charged_to_budget():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a":
            return httpx.Response(302, headers={"Location": "/b"})
        return httpx.Response(200, text="ok")

    client = make_client(handler, max_requests=1)
    with pytest.raises(RequestBudgetExceeded):
        client.get_text("https://x.test/a")
    assert client.requests_made == 1


def test_budget_exception_scope():
    client = make_client(lambda r: httpx.Response(200, text="ok"), max_requests=5, max_requests_per_host=1)
    client.request_text("https://a.example/x")
    with pytest.raises(RequestBudgetExceeded) as host_exc:
        client.request_text("https://a.example/y")
    assert host_exc.value.scope == "host" and host_exc.value.host == "a.example"
    client2 = make_client(lambda r: httpx.Response(200, text="ok"), max_requests=1)
    client2.request_text("https://a.example/x")
    with pytest.raises(RequestBudgetExceeded) as glob_exc:
        client2.request_text("https://b.example/x")
    assert glob_exc.value.scope == "global"


def test_undeclared_charset_utf8_bytes_decode_cleanly():
    handler = lambda r: httpx.Response(200, content="España – Ñ".encode(), headers={"Content-Type": "text/html"})
    assert make_client(handler).get_text("https://x.test/")[2] == "España – Ñ"


def test_wrong_latin1_charset_with_utf8_bytes_is_not_mojibake():
    handler = lambda r: httpx.Response(
        200, content="España".encode(), headers={"Content-Type": "text/html; charset=iso-8859-1"})
    assert make_client(handler).get_text("https://x.test/")[2] == "España"


def test_genuine_latin1_bytes_use_declared_charset():
    handler = lambda r: httpx.Response(
        200, content="España".encode("latin-1"), headers={"Content-Type": "text/html; charset=iso-8859-1"})
    assert make_client(handler).get_text("https://x.test/")[2] == "España"


def test_undeclared_non_utf8_falls_back_to_cp1252():
    handler = lambda r: httpx.Response(
        200, content="Engineer’s".encode("cp1252"), headers={"Content-Type": "text/html"})
    assert make_client(handler).get_text("https://x.test/")[2] == "Engineer’s"
