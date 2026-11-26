"""Select and combine keyless web providers while preserving fallback signals."""

import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from ultron.core.web_search import ddg_html, ddgs_client, searxng
from ultron.core.web_search.dedup import deduplicate_hits
from ultron.core.web_search.models import ProviderUnavailable, SearchHit, WebSearchResult
from ultron.infra.searxng import json_health

WEB_PROVIDERS = ("auto", "searxng", "ddg", "ddg_html", "multi", "none")


def _safe(search: Callable, queries: list[str], deadline: float | None) -> tuple[list[SearchHit], int]:
    try:
        return search(queries) if deadline is None else search(queries, deadline=deadline)
    except (ProviderUnavailable, ValueError):
        return [], 0


def search_web(queries: list[str], provider: str | None = None, *, deadline: float | None = None) -> WebSearchResult:
    """Search with the configured provider or auto cascade, without requiring credentials."""
    mode = provider or os.environ.get("ULTRON_WEB_SEARCH_PROVIDER") or "auto"
    if mode not in WEB_PROVIDERS:
        raise ValueError("Unknown web search provider")
    queries = queries[:40]
    if mode == "none" or not queries:
        return WebSearchResult("disabled", "none", 0, 0, ())
    if mode == "multi":
        with ThreadPoolExecutor(max_workers=2) as workers:
            local = workers.submit(_safe, searxng.search, queries, deadline)
            html, html_count = _safe(ddg_html.search, queries, deadline)
            local_hits, local_count = local.result()
        raw = local_hits + html
        return WebSearchResult("ok" if (hits := deduplicate_hits(raw)) else "unavailable",
                               "multi", len(raw), local_count + html_count, hits)
    providers = {
        "searxng": searxng.search, "ddg": ddgs_client.search, "ddg_html": ddg_html.search,
    }
    probe_timeout = 1.5 if deadline is None else min(1.5, deadline - time.monotonic())
    plan = {
        "auto": (["searxng"] if mode == "auto" and probe_timeout > 0 and json_health(timeout=probe_timeout) else [])
        + (["ddg"] if mode == "auto" and ddgs_client.available() else []) + ["ddg_html"],
        "searxng": ["searxng"], "ddg": ["ddg", "ddg_html"], "ddg_html": ["ddg_html"],
    }[mode]
    raw_count = query_count = 0
    for selected in plan:
        if deadline is not None and time.monotonic() >= deadline:
            break
        raw, count = _safe(providers[selected], queries, deadline)
        raw_count += len(raw)
        query_count += count
        if hits := deduplicate_hits(raw):
            return WebSearchResult("ok", selected, raw_count, query_count, hits)
    return WebSearchResult("unavailable", plan[-1], raw_count, query_count, ())
