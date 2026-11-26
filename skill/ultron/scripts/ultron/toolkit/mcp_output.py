"""Keep MCP dossier results inline while retaining evidence in each delivered posting."""

from __future__ import annotations

import json

MAX_INLINE_BYTES = 44_000
_EXCERPT_LIMITS = ((290, 380), (240, 300), (220, 240))
_OPTIONAL_DIAGNOSTICS = {"sources", "unresolved", "companies_without_matches", "queries"}


def encode_inline(payload: dict) -> str:
    """Serialize a payload in the same form used to measure the MCP budget."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _size(payload: dict) -> int:
    return len(encode_inline(payload).encode("utf-8"))


def _with_excerpt_limits(row: dict, geo_limit: int, role_limit: int) -> dict:
    compact = row.copy()
    for key, limit in (("geo_excerpt", geo_limit), ("role_excerpt", role_limit)):
        value = compact.get(key, "")
        if len(value) > limit:
            compact[key] = value[:limit]
    return compact


def _ranked_prefix(summary: dict, rows: list[dict]) -> dict:
    # Reserve room for the counters at their longest and charge the exact cost of each row.
    total = len(rows)
    envelope = {"returned": total, "dossiers_omitted": total, "truncated": True, "dossiers": []}
    for key, value in summary.items():
        if key != "returned" and _size({**envelope, key: value}) <= MAX_INLINE_BYTES:
            envelope[key] = value
    remaining = MAX_INLINE_BYTES - _size(envelope)
    visible = []
    for row in rows:
        size = _size(row) + bool(visible)
        if size > remaining:
            break
        remaining -= size
        visible.append(row)

    envelope.update(dossiers=visible, returned=len(visible), dossiers_omitted=total - len(visible))
    return envelope


def compact_dossiers(payload: dict, *, gather: bool = False) -> dict:
    """Keep full excerpts when they fit, otherwise shorten them or return a ranked prefix."""
    if not gather and _size(payload) <= MAX_INLINE_BYTES:
        return payload
    rows = [{key: value for key, value in row.items() if value not in ("", None)}
            for row in payload["dossiers"]]
    summary = {key: value for key, value in payload.items() if key != "dossiers"}
    if gather:
        summary.pop("queries", None)
    result = {**summary, "dossiers": rows}
    if _size(result) <= MAX_INLINE_BYTES:
        return result
    summary = {key: value for key, value in summary.items() if key not in _OPTIONAL_DIAGNOSTICS}
    if _size({**summary, "dossiers": rows}) <= MAX_INLINE_BYTES:
        return {**summary, "dossiers": rows}
    for geo_limit, role_limit in _EXCERPT_LIMITS:
        compact = [_with_excerpt_limits(row, geo_limit, role_limit) for row in rows]
        result = {**summary, "dossiers": compact}
        if _size(result) <= MAX_INLINE_BYTES:
            return result
    return _ranked_prefix(summary, compact)


def compact_gather(payload: dict) -> dict:
    """Project a gather result without its query strings or empty dossier fields."""
    return compact_dossiers(payload, gather=True)
