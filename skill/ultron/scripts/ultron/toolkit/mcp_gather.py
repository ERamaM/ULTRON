"""Keep ranked gather results within the inline MCP response budget."""

from __future__ import annotations

import json

MAX_GATHER_BYTES = 36_000
_EXCERPT_LIMITS = ((290, 380), (290, 230), (220, 180), (140, 130), (80, 80), (0, 0))


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _byte_count(text: str) -> int:
    return len(text.encode("utf-8"))


def _dossier(row: dict, geo_limit: int, role_limit: int) -> dict:
    compact = {key: value for key, value in row.items() if value not in ("", None)}
    for key, limit in (("geo_excerpt", geo_limit), ("role_excerpt", role_limit)):
        excerpt = compact.pop(key, "")[:limit].rstrip()
        if excerpt:
            compact[key] = excerpt
    return compact


def _ranked_prefix(summary: dict, rows: list[dict]) -> str:
    # Reserve room for the counters at their longest and charge the exact cost of each row.
    total = len(rows)
    envelope = {"returned": total, "dossiers_omitted": total, "truncated": True, "dossiers": []}
    for key in ("web_search", "inspected", "profile", "sources"):
        if key in summary and _byte_count(_json({**envelope, key: summary[key]})) <= MAX_GATHER_BYTES:
            envelope[key] = summary[key]
    remaining = MAX_GATHER_BYTES - _byte_count(_json(envelope))
    visible = []
    for row in rows:
        size = _byte_count(_json(row)) + bool(visible)
        if size > remaining:
            break
        remaining -= size
        visible.append(row)

    envelope.update(dossiers=visible, returned=len(visible), dossiers_omitted=total - len(visible))
    return _json(envelope)


def gather_text(payload: dict) -> str:
    """Encode a gather result inline, preserving ranked candidates before long excerpts."""
    rows = payload["dossiers"]
    summary = {key: value for key, value in payload.items() if key not in {"queries", "dossiers"}}
    for geo_limit, role_limit in _EXCERPT_LIMITS:
        compact = [_dossier(row, geo_limit, role_limit) for row in rows]
        text = _json({**summary, "dossiers": compact})
        if _byte_count(text) <= MAX_GATHER_BYTES:
            return text
        # Detailed unresolved URLs are optional; inspected.unresolved_aggregator keeps the count.
        if "unresolved" in summary:
            summary.pop("unresolved")
            text = _json({**summary, "dossiers": compact})
            if _byte_count(text) <= MAX_GATHER_BYTES:
                return text
    return _ranked_prefix(summary, compact)
