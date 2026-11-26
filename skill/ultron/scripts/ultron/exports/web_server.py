"""Lightweight loopback HTTP server rendering ``ultron.sqlite3`` with optional local review edits."""

from __future__ import annotations

import contextlib
import json
import secrets
import socket
import sqlite3
import webbrowser
from collections.abc import Callable
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import ultron
from ultron.exports import web_api, web_data

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    (
        "Content-Security-Policy",
        (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        ),
    ),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
)


def _static_dir() -> Path:
    return Path(ultron.__file__).resolve().parent / "exports" / "web_static"


def _load_static_routes() -> dict[str, tuple[str, bytes]]:
    folder = _static_dir()
    index = ("text/html; charset=utf-8", (folder / "index.html").read_bytes())
    routes = {"/": index, "/index.html": index}
    for name in ("app.css", "views.css", "kanban.css"):
        routes[f"/assets/{name}"] = ("text/css; charset=utf-8", (folder / name).read_bytes())
    scripts = ("app.js", "views.js", "kanban.js", "actions.js", "charts.js", "dom.js", "history.js")
    for name in scripts:
        routes[f"/assets/{name}"] = ("application/javascript; charset=utf-8", (folder / name).read_bytes())
    return routes


def validate_bind_target(host: str, port: int) -> tuple[str, int]:
    """Ensure ``host`` is strictly loopback and ``port`` is a valid TCP port."""
    clean_host = str(host or "").strip().lower()
    if clean_host not in LOOPBACK_HOSTS:
        allowed = ", ".join(sorted(LOOPBACK_HOSTS))
        raise ValueError(f"web viewer must bind to a loopback host ({allowed}), got {host!r}")
    if isinstance(port, bool) or not isinstance(port, int) or not (0 <= port <= 65535):
        raise ValueError(f"port must be between 0 and 65535, got {port!r}")
    return clean_host, port


def build_dashboard_url(base_url: str, *, run_id: str | None = None, profile: str | None = None) -> str:
    """Append optional hash filter parameters for ``run_id`` or ``profile`` to ``base_url``."""
    location = urlsplit(base_url)
    params = [location.fragment] if location.fragment else []
    if run_id:
        params.append(f"run={quote(str(run_id).strip(), safe='')}")
    if profile:
        params.append(f"profile={quote(str(profile).strip(), safe='')}")
    if not params:
        return base_url
    return urlunsplit(location._replace(fragment="&".join(params)))


class LedgerHTTPServer(ThreadingHTTPServer):
    """Threaded loopback HTTP server with clean daemon worker shutdown."""

    daemon_threads = True
    allow_reuse_address = True
    origin: str


class _IPv6LedgerServer(LedgerHTTPServer):
    address_family = socket.AF_INET6


class _LedgerRequestHandler(BaseHTTPRequestHandler):
    """Serve dashboard assets, live SQLite JSON and authorized edits."""

    protocol_version = "HTTP/1.1"

    def __init__(
        self,
        *args: Any,
        static_routes: dict[str, tuple[str, bytes]],
        db_path: Path,
        token: str | None,
        launch_grant: web_api.LaunchGrant | None,
        **kwargs: Any,
    ) -> None:
        self._static_routes = static_routes
        self._db_path = db_path
        self._token = token
        self._launch_grant = launch_grant
        super().__init__(*args, **kwargs)

    @property
    def _authority(self) -> str:
        return urlsplit(self.server.origin).netloc

    def log_message(self, format: str, *args: Any) -> None:
        """Suppress default stderr request logs so CLI stdout stays machine-readable JSON."""
        return

    def _send_bytes(
        self,
        status: HTTPStatus,
        content_type: str,
        payload: bytes,
        *,
        head_only: bool,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in extra_headers:
            self.send_header(name, value)
        if self.close_connection and not any(name == "Connection" for name, _ in extra_headers):
            self.send_header("Connection", "close")
        self.end_headers()
        if not head_only and payload:
            self.wfile.write(payload)

    def _send_json(self, status: HTTPStatus, data: dict[str, Any], *, head_only: bool) -> None:
        encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, "application/json; charset=utf-8", encoded, head_only=head_only)

    def _serve_get_or_head(self, *, head_only: bool) -> None:
        if not web_api.valid_host(self.headers, self._authority):
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "forbidden"}, head_only=head_only)
            return
        path = urlsplit(self.path).path
        static = self._static_routes.get(path)
        if static is not None:
            content_type, body = static
            if (path == "/" and not head_only and self._token is not None and self._launch_grant is not None
                    and self._launch_grant.consume(self.headers)):
                tag = f'<meta name="ultron-csrf" content="{self._token}">'.encode("ascii")
                body = body.replace(b"</head>", tag + b"\n</head>", 1)
            self._send_bytes(HTTPStatus.OK, content_type, body, head_only=head_only)
            return
        if path == "/api/health":
            self._send_json(HTTPStatus.OK, {"status": "ok"}, head_only=head_only)
            return
        if path == "/api/ledger":
            snapshot = web_data.build_ledger_snapshot(self._db_path)
            snapshot["meta"]["editable"] = self._token is not None
            self._send_json(HTTPStatus.OK, snapshot, head_only=head_only)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"}, head_only=head_only)

    def _drain_rejected_body(self) -> None:
        # A close with unread bytes can reset the socket before clients see the status.
        values = self.headers.get_all("Content-Length") or []
        length = values[0] if len(values) == 1 else ""
        if not 0 < len(length) <= 9 or not length.isascii() or not length.isdecimal():
            return
        remaining = min(int(length), web_api.MAX_BODY + 1)
        deadline = monotonic() + 0.25
        with contextlib.suppress(OSError):
            while remaining and (timeout := deadline - monotonic()) > 0:
                self.connection.settimeout(timeout)
                data = self.rfile.read1(remaining)
                if not data:
                    break
                remaining -= len(data)

    def _reject_method(self) -> None:
        self.close_connection = True
        self._send_bytes(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "application/json; charset=utf-8",
            b'{"error":"method_not_allowed"}',
            head_only=False,
            extra_headers=(("Allow", "GET, HEAD, POST" if self._token else "GET, HEAD"),),
        )
        self._drain_rejected_body()

    def do_GET(self) -> None:
        """Handle read-only ``GET`` requests for static assets and ledger JSON."""
        self._serve_get_or_head(head_only=False)

    def do_HEAD(self) -> None:
        """Handle read-only ``HEAD`` requests with identical headers and empty body."""
        self._serve_get_or_head(head_only=True)

    def do_POST(self) -> None:
        """Apply an authenticated browser edit only in editable mode."""
        self.close_connection = True
        if self._token is None:
            self._reject_method()
            return
        try:
            length = web_api.authorize(self.headers, self.server.origin, self._token)
        except web_api.MutationError as exc:
            self._send_json(exc.status, {"error": str(exc)}, head_only=False)
            self._drain_rejected_body()
            return
        try:
            self.connection.settimeout(10)
            data = web_api.apply_mutation(urlsplit(self.path).path, self.rfile, length, self._db_path)
        except web_api.MutationError as exc:
            self._send_json(exc.status, {"error": str(exc)}, head_only=False)
        except TimeoutError:
            self._send_json(HTTPStatus.REQUEST_TIMEOUT, {"error": "request_timeout"}, head_only=False)
        except (OSError, sqlite3.Error):
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "ledger_write_failed"}, head_only=False)
        else:
            self._send_json(HTTPStatus.OK, data, head_only=False)

    def do_PUT(self) -> None:
        """Reject mutating ``PUT`` requests with ``405 Method Not Allowed``."""
        self._reject_method()

    def do_PATCH(self) -> None:
        """Reject mutating ``PATCH`` requests with ``405 Method Not Allowed``."""
        self._reject_method()

    def do_DELETE(self) -> None:
        """Reject mutating ``DELETE`` requests with ``405 Method Not Allowed``."""
        self._reject_method()

    def do_OPTIONS(self) -> None:
        """Reject ``OPTIONS`` requests with ``405 Method Not Allowed``."""
        self._reject_method()


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    db_path: Path | None = None,
    editable: bool = False,
) -> tuple[LedgerHTTPServer, str, int]:
    """Bind a loopback ``LedgerHTTPServer`` and return ``(server, base_url, actual_port)``."""
    clean_host, clean_port = validate_bind_target(host, port)
    routes = _load_static_routes()
    target = Path(db_path).absolute() if db_path is not None else web_data.default_db_path().absolute()
    formatted_host = f"[{clean_host}]" if ":" in clean_host else clean_host
    token = secrets.token_urlsafe(32) if editable else None
    capability = secrets.token_urlsafe(32) if editable else None
    handler = partial(_LedgerRequestHandler, static_routes=routes, db_path=target, token=token,
                      launch_grant=web_api.LaunchGrant(capability) if capability else None)
    server_type = _IPv6LedgerServer if clean_host == "::1" else LedgerHTTPServer
    bound_host = "127.0.0.1" if clean_host == "localhost" else clean_host
    server = server_type((bound_host, clean_port), handler)
    actual_port = int(server.server_address[1])
    server.origin = urlunsplit(("http", f"{formatted_host}:{actual_port}", "", "", ""))
    launch = f"#access={capability}" if capability else ""
    return server, server.origin + "/" + launch, actual_port


def serve_ledger(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
    run_id: str | None = None,
    profile: str | None = None,
    db_path: Path | None = None,
    editable: bool = False,
    on_ready: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Start the local web viewer server and serve ``ultron.sqlite3`` until interrupted."""
    clean_host, _ = validate_bind_target(host, port)
    server, base_url, actual_port = create_server(host=clean_host, port=port, db_path=db_path, editable=editable)
    target = Path(db_path) if db_path is not None else web_data.default_db_path()
    url = build_dashboard_url(base_url, run_id=run_id, profile=profile)
    opened = False
    try:
        # Opening an editable URL through a launcher could expose its fragment in process arguments.
        if open_browser and not editable:
            opened = bool(webbrowser.open(url))
        info = {
            "url": url,
            "host": clean_host,
            "port": actual_port,
            "db_exists": target.is_file(),
            "opened_browser": opened,
            "editable": editable,
        }
        if on_ready is not None:
            on_ready(info)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
    finally:
        server.server_close()
    return info
