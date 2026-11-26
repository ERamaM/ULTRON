"""Bounded parallel queries against local or explicitly configured SearXNG."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit

from ultron.core.web_search.models import SearchHit, make_hit
from ultron.core.web_search.query_builder import relaxed_queries
from ultron.infra.net import HttpError, RequestBudgetExceeded
from ultron.infra.searxng import LifecycleError, default_url, read_json


def _page(base: str, query: str, page: int, deadline: float | None) -> list[SearchHit]:
    params = {"q": query, "format": "json", "categories": "general", "pageno": page}
    for attempt in range(2):
        try:
            if deadline is None:
                value = read_json(base, f"/search?{urlencode(params)}", timeout=8)
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                value = read_json(base, f"/search?{urlencode(params)}", timeout=min(8, remaining), deadline=deadline)
            results = value.get("results")
            if not isinstance(results, list):
                return []
            return [make_hit(row.get("title"), row.get("url"), row.get("content"), query, "searxng")
                    for row in results[:35] if isinstance(row, dict)]
        except HttpError as error:
            if attempt or (error.status_code is not None and error.status_code < 500):
                return []
            time.sleep(0.2)
        except (ValueError, RequestBudgetExceeded):
            return []
    return []


def _initial(base: str, query: str, deadline: float | None) -> tuple[list[SearchHit], int]:
    if deadline is not None and time.monotonic() >= deadline:
        return [], 0
    return _page(base, query, 1, deadline), 1


def _extend(base: str, query: str, first: list[SearchHit], max_pages: int, deadline: float | None) -> list[SearchHit]:
    if first:
        return [*first, *_page(base, query, 2, deadline)] if max_pages == 2 else first
    for variant in relaxed_queries(query):
        if hits := _page(base, variant, 1, deadline):
            if max_pages == 2:
                hits.extend(_page(base, variant, 2, deadline))
            return hits
    return []

def search(queries: list[str], *, max_pages: int = 1, deadline: float | None = None) -> tuple[list[SearchHit], int]:
    """Run SearXNG queries with four workers and at most two pages per variant."""
    try:
        base = os.environ.get("ULTRON_SEARXNG_URL") or default_url()
    except LifecycleError as error:
        raise ValueError(str(error)) from error
    endpoint = urlsplit(base)
    if endpoint.path not in {"", "/"}:
        raise ValueError("SearXNG URL must be an origin")
    with ThreadPoolExecutor(max_workers=4) as workers:
        # Give every original query a chance before spending the remaining budget on variants or page two.
        first = list(workers.map(lambda query: _initial(base, query, deadline), queries))

        def extend(item: tuple[str, tuple[list[SearchHit], int]]) -> list[SearchHit]:
            query, (hits, attempted) = item
            return _extend(base, query, hits, min(2, max_pages), deadline) if attempted else []

        batches = workers.map(extend, zip(queries, first, strict=True))
        return [hit for batch in batches for hit in batch], sum(attempted for _, attempted in first)
