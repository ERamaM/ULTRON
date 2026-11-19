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


_DONE = object()


def _container_children(container: dict | list) -> Iterator[object]:
    return iter(container.values() if isinstance(container, dict) else container)


def iter_json_dicts(node: object, *, max_depth: int = MAX_JSON_DEPTH, max_nodes: int = MAX_JSON_NODES) -> Iterator[dict]:
    """Yield every dict in ``node`` in pre-order, iteratively, within depth and node caps.

    Only dicts and lists count toward ``max_nodes``; scalar values are never visited. Children are read lazily,
    one iterator per depth level, so pending state is bounded by the depth, not by a container's width.
    """
    if not isinstance(node, (dict, list)) or max_nodes <= 0:
        return
    visited = 1
    if isinstance(node, dict):
        yield node
    frames: list[tuple[Iterator[object], int]] = [(_container_children(node), 1)] if max_depth >= 1 else []
    while frames:
        children, depth = frames[-1]
        child = next(children, _DONE)
        if child is _DONE:
            frames.pop()
            continue
        if not isinstance(child, (dict, list)):
            continue
        if visited >= max_nodes:
            return
        visited += 1
        if isinstance(child, dict):
            yield child
        if depth < max_depth:
            frames.append((_container_children(child), depth + 1))
