"""Common types for keyless search and its fallback signal."""

from dataclasses import dataclass

from ultron.core.extractors.text import collapse_whitespace


class ProviderUnavailable(RuntimeError):
    """A search provider cannot process the request."""


@dataclass(frozen=True, slots=True)
class SearchHit:
    """Bounded search result from a single query and provider."""

    title: str
    url: str
    snippet: str
    query: str
    provider: str


def make_hit(title: object, url: object, snippet: object, query: str, provider: str) -> SearchHit:
    """Bound untrusted provider text and retain the original URL for validation."""
    def clean(value: object, limit: int) -> str:
        return collapse_whitespace(value[:limit * 2])[:limit] if isinstance(value, str) else ""

    safe_url = url.strip() if isinstance(url, str) and len(url) <= 4096 else ""
    return SearchHit(clean(title, 250), safe_url, clean(snippet, 600), query, provider)


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    """Results and counters for a complete provider run."""

    status: str
    provider_used: str
    raw_hits: int
    queries_run: int
    hits: tuple[SearchHit, ...]

    def metadata(self) -> dict:
        """Return the JSON-safe discovery summary without individual hits."""
        return {
            "provider_used": self.provider_used,
            "status": self.status,
            "queries_run": self.queries_run,
            "raw_hits": self.raw_hits,
            "unique_hits": len(self.hits),
            "fallback_to_agent_web_search": self.status != "disabled" and not self.hits,
        }
