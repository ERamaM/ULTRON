"""Safe HTTP client: SSRF checks on every hop, request budgets, throttling, retries, size cap."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin, urlsplit

import httpx

from ultron import __version__
from ultron.core.urlpolicy import Resolver, URLRejected, admit_url

USER_AGENT = f"ULTRON/{__version__}"
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 10.0
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class RequestBudgetExceeded(RuntimeError):
    """Raised when the global or per-host request budget is spent.

    ``scope`` is ``"global"`` or ``"host"`` (then ``host`` names the host).
    """

    def __init__(self, message: str = "", *, scope: str = "global", host: str = "") -> None:
        super().__init__(message)
        self.scope = scope
        self.host = host


class HttpError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _is_transient(exc: HttpError, method: str) -> bool:
    """Connect failures never reached the server, so any method may retry; read timeouts only for safe methods."""
    cause = exc.__cause__
    if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(cause, httpx.ReadTimeout) and method in {"GET", "HEAD"}


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw.strip())
    except ValueError:
        return None
    if 0 <= value <= MAX_RETRY_AFTER:
        return value
    return None


def _decode_body(raw: bytes, declared: str | None) -> str:
    """UTF-8 strict first (a wrong declared charset must not produce mojibake), then the declared one, then cp1252."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if exc.reason == "unexpected end of data":  # body truncated mid-character: still UTF-8
            return raw[: exc.start].decode("utf-8")
    if not declared:
        return raw.decode("cp1252", errors="replace")
    if declared.lower().replace("_", "-") in {"utf-8", "utf8"}:
        return raw.decode("utf-8", errors="replace")
    try:
        return raw.decode(declared)
    except (LookupError, UnicodeDecodeError):
        return raw.decode("cp1252", errors="replace")


class HttpClient:
    def __init__(
        self,
        *,
        max_requests: int,
        max_requests_per_host: int,
        max_bytes: int = 5_000_000,
        timeout: float = 20.0,
        min_interval_per_host: float = 0.5,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_requests = max_requests
        self.max_requests_per_host = max_requests_per_host
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.min_interval_per_host = min_interval_per_host
        self.requests_made = 0
        self._resolver = resolver
        self._sleep = sleep
        self._clock = clock
        self._per_host: dict[str, int] = {}
        self._last_request: dict[str, float] = {}
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # -- internals -------------------------------------------------------

    def _charge(self, host: str) -> None:
        if self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(f"global request budget of {self.max_requests} exhausted", scope="global")
        if self._per_host.get(host, 0) >= self.max_requests_per_host:
            raise RequestBudgetExceeded(f"request budget of {self.max_requests_per_host} exhausted for {host}", scope="host", host=host)
        self.requests_made += 1
        self._per_host[host] = self._per_host.get(host, 0) + 1

    def _throttle(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None and self.min_interval_per_host > 0:
            remaining = self.min_interval_per_host - (self._clock() - last)
            if remaining > 0:
                self._sleep(remaining)
        self._last_request[host] = self._clock()

    def _send_once(
        self, method: str, url: str, host: str, timeout: float, json_body: Any, max_bytes: int,
    ) -> tuple[int, httpx.Headers, str]:
        self._charge(host)
        self._throttle(host)
        kwargs: dict[str, Any] = {"timeout": timeout}
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            with self._client.stream(method, url, **kwargs) as response:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) >= max_bytes:
                        del body[max_bytes:]
                        break
                encoding = response.charset_encoding
                headers = response.headers
                status = response.status_code
        except httpx.HTTPError as exc:
            raise HttpError(f"request failed: {type(exc).__name__}") from exc
        raw = bytes(body)
        return status, headers, _decode_body(raw, encoding)

    def _fetch(
        self, method: str, url: str, timeout: float | None, json_body: Any = None,
        allowed_domains: tuple[str, ...] = (), *, max_bytes: int | None = None,
    ) -> tuple[int, str, str]:
        effective_timeout = self.timeout if timeout is None else timeout
        byte_limit = self.max_bytes if max_bytes is None else max_bytes
        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            admitted = admit_url(
                current, allowed_domains=allowed_domains, resolve_dns=True, resolver=self._resolver
            )
            current = admitted.url
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    status, headers, text = self._send_once(
                        method, current, admitted.hostname, effective_timeout, json_body, byte_limit,
                    )
                except HttpError as exc:
                    if attempt < MAX_ATTEMPTS and _is_transient(exc, method):
                        self._sleep(0.5 * attempt)
                        continue
                    raise
                if status in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                    delay = _retry_after(httpx.Response(status, headers=headers))
                    self._sleep(0.5 * attempt if delay is None else delay)
                    continue
                break
            location = headers.get("Location")
            if status in REDIRECT_STATUSES and location:
                current = urljoin(current, location)
                if status in {301, 302, 303}:
                    method, json_body = "GET", None
                continue
            return status, current, text
        raise HttpError(f"too many redirects (max {MAX_REDIRECTS})")

    # -- public API ------------------------------------------------------

    def get_text(self, url: str) -> tuple[int, str, str]:
        return self._fetch("GET", url, None)

    def request_text(
        self, url: str, timeout: float | None = None, *, allowed_domains: tuple[str, ...] = ()
    ) -> str:
        """``allowed_domains`` (host suffixes) is enforced on every redirect hop; empty means any public host."""
        status, final_url, text = self._fetch("GET", url, timeout, allowed_domains=allowed_domains)
        if status >= 300:
            raise HttpError(f"HTTP {status} for {urlsplit(final_url).netloc}", status_code=status)
        return text

    def request_json(
        self,
        url: str,
        method: str = "GET",
        json_body: Any = None,
        timeout: float | None = None,
        *,
        allowed_domains: tuple[str, ...] = (),
        max_bytes: int | None = None,
    ) -> dict | list:
        status, final_url, text = self._fetch(
            method.upper(), url, timeout, json_body, allowed_domains, max_bytes=max_bytes,
        )
        if status >= 300:
            raise HttpError(f"HTTP {status} for {urlsplit(final_url).netloc}", status_code=status)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise HttpError("response was not valid JSON", status_code=status) from exc


__all__ = ["HttpClient", "HttpError", "RequestBudgetExceeded", "URLRejected"]
