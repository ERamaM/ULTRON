"""Normalize and deduplicate URLs returned by web search."""

from dataclasses import replace
from urllib.parse import urlsplit

from ultron.core.discovery.url_filter import normalize_url
from ultron.core.web_search.models import SearchHit
from ultron.infra.urlpolicy import admit_url


def deduplicate_hits(hits: list[SearchHit]) -> tuple[SearchHit, ...]:
    """Keep the first result per normalized, syntactically safe public URL."""
    unique = {}
    for hit in hits:
        try:
            if urlsplit(hit.url).scheme not in {"http", "https"}:
                continue
            url = normalize_url(admit_url(hit.url).url)
        except ValueError:
            continue
        unique.setdefault(url, replace(hit, url=url))
    return tuple(unique.values())
