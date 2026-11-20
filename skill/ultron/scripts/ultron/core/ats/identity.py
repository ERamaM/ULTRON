"""Canonical board identity shared by inventory readers and writers."""

from __future__ import annotations

import re

# Vendors whose board identifier is an opaque, case-sensitive token (an Occupop share token, CandidateManager ids).
OPAQUE_CASE_SENSITIVE_VENDORS = frozenset({"occupop", "candidatemanager"})


def seed_identity(vendor: str, board: str) -> tuple[str, str]:
    """Fold vendor tokens, retaining Workday's case-sensitive site path and opaque case-sensitive board tokens."""
    vendor = vendor.strip().lower()
    board = board.strip().strip("/")
    if vendor == "workday":
        host, separator, site = board.partition("/")
        parts = site.split("/")
        if len(parts) > 1 and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
            site = "/".join(parts[1:])
        return vendor, host.lower() + separator + site
    if vendor in OPAQUE_CASE_SENSITIVE_VENDORS:
        return vendor, board
    return vendor, board.lower()
