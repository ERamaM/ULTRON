"""Optional adapter for the keyless ddgs Python package."""

import time
from concurrent.futures import ThreadPoolExecutor

from ultron.core.web_search.models import ProviderUnavailable, SearchHit, make_hit


def available() -> bool:
    """Return whether the optional provider can be imported."""
    try:
        from ddgs import DDGS  # noqa: F401 - the optional import is the probe
    except ImportError:
        return False
    return True


def search(queries: list[str], *, deadline: float | None = None) -> tuple[list[SearchHit], int]:
    """Return results from the optional search package."""
    try:
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException
    except ImportError as error:
        raise ProviderUnavailable("Install ultron[search] to enable ddgs") from error
    def find(query: str) -> tuple[list[SearchHit], int]:
        timeout = 4.0 if deadline is None else min(4.0, deadline - time.monotonic())
        if timeout <= 0:
            return [], 0
        try:
            rows = DDGS(timeout=timeout).text(query, max_results=15)
        except DDGSException:
            return [], 1
        return [make_hit(row.get("title"), row.get("href"), row.get("body"), query, "ddg")
                for row in rows if isinstance(row, dict)], 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        hits, count = [], 0
        for rows, attempted in pool.map(find, queries):
            hits.extend(rows)
            count += attempted
    return hits, count
