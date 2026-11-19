"""Spec §5.7 / FR-1: ATS board identifiers are validated per vendor and API calls stay on the vendor's hosts."""

from __future__ import annotations

import re

from ultron.core.ats.vendors import VENDOR_DATA_BY_NAME, domain_matches
from ultron.core.urlpolicy import admit_url
from ultron.net import HttpClient

# These APIs include both HTML and plaintext descriptions for every job in the board.
_DESCRIPTION_HEAVY_BOARD_MAX_BYTES = 20_000_000

# Vendors whose board identifier is ``host[/path]`` (the client calls that host directly).
HOST_STYLE_VENDORS = frozenset({"workday", "icims", "taleo"})
_SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*", re.ASCII)
_HOST_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+", re.ASCII)
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._~-]+", re.ASCII)


def vendor_domains(vendor: str) -> tuple[str, ...]:
    """Host suffixes the vendor's API calls may reach (``()`` for an unknown vendor)."""
    item = VENDOR_DATA_BY_NAME.get(str(vendor or "").strip().lower())
    return item.all_domains if item else ()


def board_is_valid(vendor: str, board: str) -> bool:
    """``board`` is safe to build the vendor's URLs from: a plain slug, or a vendor host plus a clean path."""
    vendor = str(vendor or "").strip().lower()
    domains = vendor_domains(vendor)
    if not domains or not isinstance(board, str) or ".." in board:
        return False
    if vendor not in HOST_STYLE_VENDORS:
        return bool(_SLUG_RE.fullmatch(board))
    host, _, path = board.partition("/")
    host = host.lower()
    if not _HOST_RE.fullmatch(host) or not any(domain_matches(host, d) for d in domains):
        return False
    segments = path.strip("/").split("/") if path.strip("/") else []
    return all(_SEGMENT_RE.fullmatch(segment) for segment in segments)


class VendorScopedClient:
    """Client view for one vendor: every request (and, on ``HttpClient``, every redirect hop) must stay on
    the vendor's host suffixes. Budgets and throttling are the wrapped client's."""

    def __init__(self, client, vendor: str) -> None:
        self._client = client
        self._json_max_bytes = _DESCRIPTION_HEAVY_BOARD_MAX_BYTES if vendor in {"ashby", "lever"} else None
        self.allowed_domains = vendor_domains(vendor)

    def _admit(self, url: str) -> dict:
        admit_url(url, allowed_domains=self.allowed_domains)  # no DNS here; the real client resolves
        return {"allowed_domains": self.allowed_domains} if isinstance(self._client, HttpClient) else {}

    def request_json(self, url: str, method: str = "GET", json_body=None, timeout: float | None = None):
        extra = self._admit(url)
        if extra and self._json_max_bytes is not None:
            extra["max_bytes"] = self._json_max_bytes
        return self._client.request_json(url, method=method, json_body=json_body, timeout=timeout, **extra)

    def request_text(self, url: str, timeout: float | None = None) -> str:
        extra = self._admit(url)
        return self._client.request_text(url, timeout=timeout, **extra)
