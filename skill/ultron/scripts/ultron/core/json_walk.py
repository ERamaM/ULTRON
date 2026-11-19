"""Bounded helpers for untrusted JSON embedded in fetched pages (JSON-LD, ``__NEXT_DATA__``)."""

from __future__ import annotations

import json
from collections.abc import Iterator

# Real job payloads are shallow; deeper or larger structures are hostile or irrelevant.
MAX_JSON_DEPTH = 256
MAX_JSON_NODES = 200_000


def load_json_or_none(text: str) -> object | None:
    """Parse ``text``; return None for invalid JSON or nesting too deep for the decoder."""
    try:
        return json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        return None


def iter_json_dicts(node: object, *, max_depth: int = MAX_JSON_DEPTH, max_nodes: int = MAX_JSON_NODES) -> Iterator[dict]:
    """Yield every dict in ``node`` in pre-order, iteratively, within depth and node caps.

    Only dicts and lists count toward ``max_nodes``; scalar values are never visited.
    """
    stack: list[tuple[object, int]] = [(node, 0)] if isinstance(node, (dict, list)) else []
    visited = 0
    while stack and visited < max_nodes:
        current, depth = stack.pop()
        visited += 1
        if isinstance(current, dict):
            yield current
            children = list(current.values())
        else:
            children = current
        if depth < max_depth:
            stack.extend((child, depth + 1) for child in reversed(children) if isinstance(child, (dict, list)))
