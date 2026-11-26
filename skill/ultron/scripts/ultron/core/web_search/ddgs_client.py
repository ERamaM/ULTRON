"""Optional adapter for the keyless ddgs Python package."""

from ultron.core.web_search.models import ProviderUnavailable, SearchHit, make_hit


def available() -> bool:
    """Return whether the optional provider can be imported."""
    try:
        from ddgs import DDGS  # noqa: F401 - the optional import is the probe
    except ImportError:
        return False
    return True


def search(queries: list[str]) -> tuple[list[SearchHit], int]:
    """Return results from the optional search package."""
    try:
        from ddgs import DDGS
        from ddgs.exceptions import DDGSException
    except ImportError as error:
        raise ProviderUnavailable("Install ultron[search] to enable ddgs") from error
    hits = []
    client = DDGS(timeout=4)
    for query in queries:
        try:
            rows = client.text(query, max_results=15)
        except DDGSException:
            continue
        hits.extend(make_hit(row.get("title"), row.get("href"), row.get("body"), query, "ddg")
                    for row in rows if isinstance(row, dict))
    return hits, len(queries)
