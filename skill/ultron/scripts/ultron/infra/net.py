"""Safe HTTP client: SSRF checks on every hop, request budgets, throttling, retries, size cap."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from datetime import UTC
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin, urlsplit

import httpx

from ultron import __version__
from ultron.infra.pinned_transport import PINNED_ADDRESSES, PinnedTransport
from ultron.infra.urlpolicy import Resolver, URLRejected, admit_url, resolve_public_addresses

USER_AGENT = f"ULTRON/{__version__}"
MAX_REDIRECTS = 5
MAX_ATTEMPTS = 3
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
MAX_RETRY_AFTER = 10.0
# Adaptive per-host delay: a 429 doubles it (at least 5s, at most 15s); each 2xx lowers it by 0.5s.
HOST_BACKOFF_MIN = 5.0
HOST_BACKOFF_MAX = 15.0
HOST_BACKOFF_DECAY = 0.5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class RequestBudgetExceeded(RuntimeError):
    """Raised when the global or per-host request budget is spent.

    ``scope`` is ``"global"``, ``"host"`` (then ``host`` names the host) or ``"vendor"`` (then ``vendor`` names
    the ATS vendor whose run budget is spent and ``host`` the tenant host that was about to be called).
    """

    def __init__(self, message: str = "", *, scope: str = "global", host: str = "", vendor: str = "") -> None:
        super().__init__(message)
        self.scope = scope
        self.host = host
        self.vendor = vendor


class HttpError(RuntimeError):
    """Raised when an HTTP request fails transport, TLS, status, or size checks."""
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HostBlocked(HttpError):
    """The host answered 403/429 ``block_threshold`` times in a row; no further request is sent to it."""

    def __init__(self, host: str, message: str = "") -> None:
        super().__init__(message or f"host_blocked: {host}")
        self.host = host


class VendorBlocked(HostBlocked):
    """Hosts of this ATS vendor answered 403/429 too often in this run; no host of the vendor is called again.

    ``host`` is the host that was about to be called; ``vendor`` names the blocked vendor."""

    def __init__(self, vendor: str, host: str) -> None:
        super().__init__(host, f"vendor_blocked: {vendor}")
        self.vendor = vendor


class VendorBudget:
    """Requests and blocked answers per ATS vendor for one run, shared by every client (and stage) of that run.

    A multi-tenant vendor serves each customer from its own host (``acme.wd5.myworkdayjobs.com``), so per-host caps
    and breakers never bound the vendor as a whole. Here every host of a vendor counts toward one cap
    (``max_requests``) and one breaker. Strikes count per request, not per retry attempt: a request that met a 429 is
    a strike for the rest of the run; a request that ended in 403 is a strike until the vendor next answers 2xx (a
    single private tenant must not block the vendor). ``block_threshold`` strikes block the vendor. ``on_change`` is
    called after every change (persistence).
    """

    def __init__(
        self, *, max_requests: int | None, block_threshold: int = 2, state: Mapping[str, Any] | None = None,
        on_change: Callable[[VendorBudget], None] | None = None,
    ) -> None:
        self.max_requests = max_requests
        self.block_threshold = block_threshold
        self.on_change = on_change
        state = state if isinstance(state, Mapping) else {}
        self.requests: dict[str, int] = _int_map(state.get("requests"))
        self.rate_limited: dict[str, int] = _int_map(state.get("rate_limited"))
        self.forbidden_streak: dict[str, int] = _int_map(state.get("forbidden_streak"))
        blocked = state.get("blocked")
        self.blocked: set[str] = {v for v in blocked if isinstance(v, str)} if isinstance(blocked, list) else set()
        # Clients in parallel threads may share one budget: its counters change under this lock.
        self._lock = threading.RLock()

    def to_dict(self) -> dict[str, Any]:
        """Export request, rate-limit, and block counters as a sorted dictionary."""
        return {
            "requests": dict(sorted(self.requests.items())),
            "rate_limited": dict(sorted(self.rate_limited.items())),
            "forbidden_streak": dict(sorted(self.forbidden_streak.items())),
            "blocked": sorted(self.blocked),
        }

    def strikes(self, vendor: str) -> int:
        """Return total rate-limit and consecutive forbidden strikes for ``vendor``."""
        return self.rate_limited.get(vendor, 0) + self.forbidden_streak.get(vendor, 0)

    def is_blocked(self, vendor: str) -> bool:
        """Return True if ``vendor`` has been blocked for this client."""
        return vendor in self.blocked

    def remaining(self, vendor: str) -> int | None:
        """Return remaining request budget for ``vendor``, or None if unlimited."""
        if self.max_requests is None:
            return None
        return max(0, self.max_requests - self.requests.get(vendor, 0))

    def charge(self, vendor: str) -> None:
        """Increment request count for ``vendor`` and notify any observer."""
        with self._lock:
            self.requests[vendor] = self.requests.get(vendor, 0) + 1
            self._changed()

    def record(self, vendor: str, status: int, *, rate_limited: bool = False) -> None:
        """Count one request's outcome: its final ``status``, and ``rate_limited`` if any attempt got a 429."""
        with self._lock:
            self._record(vendor, status, rate_limited)

    def _record(self, vendor: str, status: int, rate_limited: bool) -> None:
        changed = False
        if rate_limited or status == 429:
            self.rate_limited[vendor] = self.rate_limited.get(vendor, 0) + 1
            changed = True
        if status == 403:
            self.forbidden_streak[vendor] = self.forbidden_streak.get(vendor, 0) + 1
            changed = True
        elif 200 <= status < 300 and self.forbidden_streak.get(vendor):
            # Any final success clears the 403 streak, also when an earlier attempt of this request met a 429.
            self.forbidden_streak[vendor] = 0
            changed = True
        if not changed:
            return
        if self.strikes(vendor) >= self.block_threshold:
            self.blocked.add(vendor)
        self._changed()

    def _changed(self) -> None:
        if self.on_change is not None:
            self.on_change(self)


def _int_map(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool) and v >= 0}


def _is_transient(exc: HttpError, method: str) -> bool:
    """Connect failures never reached the server, so any method may retry; read timeouts only for safe methods."""
    cause = exc.__cause__
    if isinstance(cause, (httpx.ConnectError, httpx.ConnectTimeout)):
        return True
    return isinstance(cause, httpx.ReadTimeout) and method in {"GET", "HEAD"}


def _retry_after(response: httpx.Response, now: float) -> float | None:
    """Seconds from ``Retry-After`` (delta-seconds or HTTP-date against wall-clock ``now``), capped."""
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:  # "-0000" zone: RFC 5322 says UTC
                when = when.replace(tzinfo=UTC)
            value = max(when.timestamp() - now, 0.0)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    if not value >= 0:  # NaN or negative delta-seconds
        return None
    return min(value, MAX_RETRY_AFTER)


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


TOP_HOSTS_REPORTED = 5
_VENDOR_KEY = "vendor:"  # throttle/delay key prefix for a vendor; never a hostname (hostnames have no colon)


def top_hosts(*counts: Mapping[str, int], limit: int = TOP_HOSTS_REPORTED) -> list[dict[str, Any]]:
    """The ``limit`` hosts with most requests over one or more per-host counts, busiest first (ties by name)."""
    merged: dict[str, int] = {}
    for count in counts:
        for host, requests in count.items():
            merged[host] = merged.get(host, 0) + requests
    ranked = sorted(merged.items(), key=lambda item: (-item[1], item[0]))
    return [{"host": host, "requests": requests} for host, requests in ranked[:limit]]


class HostGate:
    """Shared by clients running in parallel threads: one request at a time per host, spaced across all of them.

    Each client keeps its own budgets; the gate only makes sure two threads never hit the same host at once and
    that the host's spacing holds across clients.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}

    def lock_for(self, host: str) -> threading.Lock:
        """The lock of ``host`` (created on first use)."""
        with self._guard:
            return self._locks.setdefault(host, threading.Lock())

    def space(self, host: str, interval: float, clock: Callable[[], float], sleep: Callable[[float], None]) -> None:
        """Wait until ``interval`` seconds have passed since the last request to ``host`` by any client."""
        last = self._last.get(host)
        if last is not None and interval > 0:
            wait = interval - (clock() - last)
            if wait > 0:
                sleep(wait)
        self._last[host] = clock()


class HttpClient:
    """SSRF-guarded, budget-limited HTTP client wrapping a pinned transport."""
    def __init__(
        self,
        *,
        max_requests: int,
        max_requests_per_host: int,
        max_bytes: int = 5_000_000,
        timeout: float = 20.0,
        min_interval_per_host: float = 0.5,
        gate: HostGate | None = None,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        block_threshold: int | None = None,
        vendor_of: Callable[[str], str | None] | None = None,
        vendor_budget: VendorBudget | None = None,
        vendor_share: float = 1.0,
        min_interval_per_vendor: float = 0.0,
        vendor_intervals: Mapping[str, float] | None = None,
    ) -> None:
        # With a threshold, a host whose consecutive 403/429 streak reached it gets no more requests from this
        # client: no retry, no redirect hop and no fallback call to it (they raise HostBlocked instead).
        self.block_threshold = block_threshold
        self.max_requests = max_requests
        self.max_requests_per_host = max_requests_per_host
        self.max_bytes = max_bytes
        self.timeout = timeout
        self.min_interval_per_host = min_interval_per_host
        self._gate = gate
        self.requests_made = 0
        self._resolver = resolver
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock
        self._per_host: dict[str, int] = {}
        self._last_request: dict[str, float] = {}
        self._host_delay: dict[str, float] = {}
        self._block_streak: dict[str, int] = {}
        self._validated: dict[str, tuple[str, ...]] = {}  # hostname -> addresses validated for the current hop
        # Vendor level (``vendor_of`` maps a host to its ATS vendor, or None): the run-wide ``vendor_budget`` caps and
        # blocks every tenant host of a vendor together; this client stops at ``vendor_share`` of the run's cap.
        # Requests to hosts of one vendor are also spaced by the vendor's interval, whichever tenant they go to.
        self._vendor_of = vendor_of
        self.vendor_budget = vendor_budget
        self.vendor_share = vendor_share
        self.min_interval_per_vendor = min_interval_per_vendor
        self._vendor_intervals = dict(vendor_intervals or {})
        self._vendor_spent: dict[str, int] = {}
        self._client = httpx.Client(
            # An injected transport (tests) sees the logical URL; the pinned addresses travel in the extension.
            transport=PinnedTransport() if transport is None else transport,
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
        """Close the underlying HTTP transport pools."""
        self._client.close()

    # -- internals -------------------------------------------------------

    def _charge(self, host: str) -> None:
        if self.requests_made >= self.max_requests:
            raise RequestBudgetExceeded(f"global request budget of {self.max_requests} exhausted", scope="global")
        if self._per_host.get(host, 0) >= self.max_requests_per_host:
            raise RequestBudgetExceeded(f"request budget of {self.max_requests_per_host} exhausted for {host}", scope="host", host=host)
        self.requests_made += 1
        self._per_host[host] = self._per_host.get(host, 0) + 1

    def vendor_for(self, host: str) -> str | None:
        """The ATS vendor ``host`` belongs to, when a vendor budget applies to this client."""
        return self._vendor_of(host) if self._vendor_of is not None and self.vendor_budget is not None else None

    def vendor_allowance(self, vendor: str) -> int | None:
        """Requests this client may still send to ``vendor`` (``None``: no vendor budget applies).

        ``vendor_share`` is a ceiling on the run's total: this client sends nothing more to the vendor once the run
        has spent ``vendor_share`` of the cap (stages before fetch stop at half, so fetch always keeps a part)."""
        budget = self.vendor_budget
        if budget is None or budget.max_requests is None:
            return None
        ceiling = int(budget.max_requests * self.vendor_share)
        return max(0, ceiling - budget.requests.get(vendor, 0))

    def requests_by_vendor(self) -> dict[str, int]:
        """Requests this client sent to each ATS vendor (a copy)."""
        return dict(self._vendor_spent)

    def vendors_blocked(self) -> list[str]:
        """Vendors the run's breaker has blocked so far."""
        return sorted(self.vendor_budget.blocked) if self.vendor_budget is not None else []

    def requests_by_host(self) -> dict[str, int]:
        """Requests charged so far, per host (a copy)."""
        return dict(self._per_host)

    def host_delay(self, host: str) -> float:
        """Current minimum spacing between requests to ``host`` (adapts to 429 and 2xx responses)."""
        return self._host_delay.get(host, self._base_interval(host))

    def _base_interval(self, key: str) -> float:
        if key.startswith(_VENDOR_KEY):
            return self._vendor_intervals.get(key[len(_VENDOR_KEY):], self.min_interval_per_vendor)
        return self.min_interval_per_host

    def block_streak(self, host: str) -> int:
        """Consecutive 403/429 responses from ``host``, counting every attempt (retries included)."""
        return self._block_streak.get(host, 0)

    def _adapt_host_delay(self, host: str, status: int) -> None:
        self._block_streak[host] = self._block_streak.get(host, 0) + 1 if status in (403, 429) else 0
        self._adapt_delay(host, status)

    def _adapt_delay(self, host: str, status: int) -> None:
        current = self.host_delay(host)
        if status == 429:
            self._host_delay[host] = min(HOST_BACKOFF_MAX, max(HOST_BACKOFF_MIN, current * 2.0))
        elif 200 <= status < 300 and current > self._base_interval(host):
            self._host_delay[host] = max(self._base_interval(host), current - HOST_BACKOFF_DECAY)

    def _throttle(self, *keys: str) -> None:
        """One wait until every key's spacing has passed (host and vendor), then stamp all keys with the send time."""
        now = self._clock()
        wait = 0.0
        for key in keys:
            last = self._last_request.get(key)
            interval = self.host_delay(key)
            if last is not None and interval > 0:
                wait = max(wait, interval - (now - last))
        if wait > 0:
            self._sleep(wait)
        sent = self._clock()
        for key in keys:
            self._last_request[key] = sent

    def _send_once(
        self, method: str, url: str, host: str, timeout: float, json_body: Any, max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, httpx.Headers, str]:
        """Send one request; with a shared gate, never in parallel with another client's request to ``host`` or to
        another tenant of its vendor, and spaced by both intervals across clients."""
        if self._gate is None:
            return self._send_unlocked(method, url, host, timeout, json_body, max_bytes, headers)
        vendor = self.vendor_for(host)
        keys = sorted({host, *((_VENDOR_KEY + vendor,) if vendor else ())})  # one lock order: no deadlock
        with ExitStack() as stack:
            for key in keys:
                stack.enter_context(self._gate.lock_for(key))
            for key in keys:
                self._gate.space(key, self.host_delay(key), self._clock, self._sleep)
            return self._send_unlocked(method, url, host, timeout, json_body, max_bytes, headers)

    def _send_unlocked(
        self, method: str, url: str, host: str, timeout: float, json_body: Any, max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, httpx.Headers, str]:
        vendor = self.vendor_for(host)
        if vendor is not None:
            if self.vendor_budget.is_blocked(vendor):
                raise VendorBlocked(vendor, host)
            if self.vendor_allowance(vendor) == 0:
                raise RequestBudgetExceeded(
                    f"request budget exhausted for vendor {vendor}", scope="vendor", host=host, vendor=vendor,
                )
        if self.block_threshold is not None and self.block_streak(host) >= self.block_threshold:
            raise HostBlocked(host)
        self._charge(host)
        if vendor is not None:
            self._vendor_spent[vendor] = self._vendor_spent.get(vendor, 0) + 1
            self.vendor_budget.charge(vendor)
            self._throttle(host, _VENDOR_KEY + vendor)
        else:
            self._throttle(host)
        kwargs: dict[str, Any] = {"timeout": timeout, "extensions": {PINNED_ADDRESSES: self._validated.get(host, ())}}
        if json_body is not None:
            kwargs["json"] = json_body
        if headers:
            kwargs["headers"] = dict(headers)
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
        except httpx.InvalidURL as exc:  # e.g. a host Python's IDNA codec accepts but httpx (IDNA2008) refuses
            raise URLRejected("invalid_url") from exc
        except httpx.HTTPError as exc:
            raise HttpError(f"request failed: {type(exc).__name__}") from exc
        self._adapt_host_delay(host, status)
        if vendor is not None:
            self._adapt_delay(_VENDOR_KEY + vendor, status)
        raw = bytes(body)
        return status, headers, _decode_body(raw, encoding)

    def _fetch(
        self, method: str, url: str, timeout: float | None, json_body: Any = None,
        allowed_domains: tuple[str, ...] = (), *, max_bytes: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, str, str]:
        effective_timeout = self.timeout if timeout is None else timeout
        byte_limit = self.max_bytes if max_bytes is None else max_bytes
        current = url
        first_origin: tuple[str, str, int | None] | None = None
        for _hop in range(MAX_REDIRECTS + 1):
            admitted = admit_url(current, allowed_domains=allowed_domains)
            # Resolve once per hop; the transport connects only to these validated addresses (no second lookup).
            # Resolve the ASCII host HTTPX connects to and sends as SNI/Host (IDNA2008, e.g. "xn--fa-hia.de"),
            # not the Unicode name, which Python's resolver would encode differently ("fass.de").
            try:
                connect_host = httpx.URL(admitted.url).raw_host.decode("ascii")
            except (httpx.InvalidURL, UnicodeError) as exc:
                raise URLRejected("invalid_url") from exc
            self._validated[admitted.hostname] = resolve_public_addresses(
                connect_host, admitted.port, resolver=self._resolver
            )
            current = admitted.url
            origin = (urlsplit(admitted.url).scheme, admitted.hostname, admitted.port)
            first_origin = first_origin or origin
            # Request headers (credentials) go only to the origin they were meant for (scheme, host and port), never
            # to a redirect target on another host or downgraded to plain HTTP.
            hop_headers = headers if origin == first_origin else None
            vendor = self.vendor_for(admitted.hostname)
            rate_limited = False
            try:
                for attempt in range(1, MAX_ATTEMPTS + 1):
                    try:
                        status, response_headers, text = self._send_once(
                            method, current, admitted.hostname, effective_timeout, json_body, byte_limit, hop_headers,
                        )
                    except HttpError as exc:
                        if attempt < MAX_ATTEMPTS and _is_transient(exc, method):
                            self._sleep(0.5 * attempt)
                            continue
                        raise
                    rate_limited = rate_limited or status == 429
                    if status in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                        delay = _retry_after(httpx.Response(status, headers=response_headers), self._wall_clock())
                        self._sleep(0.5 * attempt if delay is None else delay)
                        continue
                    break
            except Exception:
                # Any interruption of the retries (a transport error, a spent budget, a block) keeps a 429 the
                # request already met as a vendor strike.
                if vendor is not None and rate_limited:
                    self.vendor_budget.record(vendor, 429, rate_limited=True)
                raise
            if vendor is not None:
                self.vendor_budget.record(vendor, status, rate_limited=rate_limited)
            location = response_headers.get("Location")
            if status in REDIRECT_STATUSES and location:
                current = urljoin(current, location)
                if status in {301, 302, 303}:
                    method, json_body = "GET", None
                continue
            return status, current, text
        raise HttpError(f"too many redirects (max {MAX_REDIRECTS})")

    # -- public API ------------------------------------------------------

    def get_text(self, url: str, *, allowed_domains: tuple[str, ...] = ()) -> tuple[int, str, str]:
        """``(status, final_url, text)``; ``allowed_domains`` (host suffixes) is enforced on every redirect hop."""
        return self._fetch("GET", url, None, allowed_domains=allowed_domains)

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
        headers: Mapping[str, str] | None = None,
    ) -> dict | list:
        """Execute an HTTP request and parse the response body as JSON.

        ``headers`` (for example an API key) are sent to the requested host only, never to a redirect target.
        """
        status, final_url, text = self._fetch(
            method.upper(), url, timeout, json_body, allowed_domains, max_bytes=max_bytes, headers=headers,
        )
        if status >= 300:
            raise HttpError(f"HTTP {status} for {urlsplit(final_url).netloc}", status_code=status)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise HttpError("response was not valid JSON", status_code=status) from exc


__all__ = ["HostGate", "HttpClient", "HttpError", "RequestBudgetExceeded", "URLRejected", "VendorBlocked", "VendorBudget"]
