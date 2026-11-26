"""Bounded parallel queries against local or explicitly configured SearXNG."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode, urlsplit

from ultron.core.web_search.models import SearchHit, make_hit
from ultron.core.web_search.query_builder import relaxed_queries
from ultron.infra.net import HttpError, RequestBudgetExceeded
from ultron.infra.searxng import LifecycleError, default_url, read_json


def _page(base: str, query: str, page: int) -> list[SearchHit]:
    params = {"q": query, "format": "json", "categories": "general", "pageno": page}
    for attempt in range(2):
        try:
            value = read_json(base, f"/search?{urlencode(params)}", timeout=8)
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


def _query(base: str, query: str, max_pages: int) -> list[SearchHit]:
    for variant in (query, *relaxed_queries(query)):
        hits = _page(base, variant, 1)
        if hits:
            return [*hits, *_page(base, variant, 2)] if max_pages == 2 else hits
    return []


def search(queries: list[str], *, max_pages: int = 1) -> tuple[list[SearchHit], int]:
    """Run SearXNG queries with four workers and at most two pages per variant."""
    try:
        base = os.environ.get("ULTRON_SEARXNG_URL") or default_url()
    except LifecycleError as error:
        raise ValueError(str(error)) from error
    endpoint = urlsplit(base)
    if endpoint.path not in {"", "/"}:
        raise ValueError("SearXNG URL must be an origin")
    with ThreadPoolExecutor(max_workers=4) as workers:
        runs = workers.map(lambda query: _query(base, query, min(2, max_pages)), queries)
        return [hit for batch in runs for hit in batch], len(queries)
