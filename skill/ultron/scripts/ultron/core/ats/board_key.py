"""Resolve watch board identifiers from supported ATS URLs or vendor keys."""

from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, get_vendor_spec


def resolve_board_key(raw: str) -> tuple[str, str] | None:
    """``(seed key, company)`` of a board URL or a ``vendor:board`` key; ``None`` when it names no board."""
    text = str(raw or "").strip()
    if "://" in text:
        seed = extract_seed_from_url(text, "")
        return (":".join(seed_identity(seed.ats, seed.board_identifier)), seed.company) if seed else None
    vendor, sep, board = text.partition(":")
    if not sep or not board.strip() or get_vendor_spec(vendor.strip().lower()) is None:
        return None
    return ":".join(seed_identity(vendor.strip().lower(), board.strip())), ""
