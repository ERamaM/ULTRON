"""Read DuckDuckGo's HTML and Lite search results without extra dependencies."""

from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup

from ultron.core.web_search.models import SearchHit, make_hit
from ultron.infra.net import HttpClient, HttpError, RequestBudgetExceeded
from ultron.infra.urlpolicy import URLRejected

DOMAINS = ("html.duckduckgo.com", "lite.duckduckgo.com")
_ENDPOINTS = (f"https://{DOMAINS[0]}/html/", f"https://{DOMAINS[1]}/lite/")


def _result_url(href: str, base: str) -> str:
    if len(href) > 4096:
        return ""
    try:
        parts = urlsplit(urljoin(base, href))
    except ValueError:
        return ""
    if parts.hostname in (*DOMAINS, "duckduckgo.com", "www.duckduckgo.com"):
        if parts.path.rstrip("/") == "/l":
            return (parse_qs(parts.query).get("uddg") or [""])[0]
        return ""
    return parts.geturl() if parts.scheme in {"http", "https"} else ""


def parse_results(body: str, base: str, query: str) -> list[SearchHit]:
    """Parse organic HTML or Lite results and unwrap DuckDuckGo links."""
    soup = BeautifulSoup(body, "html.parser")
    hits = []
    pending = None
    for row in soup.select("div.result, tr"):
        if "result--ad" in row.get("class", []):
            continue
        link = row.select_one("a.result__a, a.result-link")
        snippet = row.select_one(".result__snippet, td.result-snippet")
        if link:
            if pending:
                hits.append(pending)
            url = _result_url(str(link.get("href", "")), base)
            pending = make_hit(link.get_text(" ", strip=True), url, "", query, "ddg_html") if url else None
        if pending and snippet:
            hits.append(make_hit(pending.title, pending.url, snippet.get_text(" ", strip=True), query, "ddg_html"))
            pending = None
        if len(hits) >= 35:
            return hits
    if pending:
        hits.append(pending)
    return hits


def search(queries: list[str]) -> tuple[list[SearchHit], int]:
    """Search serially with host spacing and a ten-query cap."""
    queries = queries[:10]
    hits = []
    with HttpClient(max_requests=30, max_requests_per_host=12, timeout=4, max_bytes=1_000_000,
                    min_interval_per_host=1.5, block_threshold=1) as client:
        for query in queries:
            for base in _ENDPOINTS:
                try:
                    status, final, body = client.get_text(f"{base}?{urlencode({'q': query})}", allowed_domains=DOMAINS)
                    results = parse_results(body, final, query) if status == 200 else []
                except (HttpError, RequestBudgetExceeded, URLRejected):
                    continue
                if results:
                    hits.extend(results)
                    break
    return hits, len(queries)
