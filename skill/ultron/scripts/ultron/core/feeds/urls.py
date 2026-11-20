from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import AtsHTTPClient
from ultron.core.discovery.title_vocabulary import TitleVocabulary
from ultron.core.rules.term_matching import find_terms
from ultron.net import HostBlocked, RequestBudgetExceeded

URL_RE = r"https?://[^\s\"'<>]+"
AGGREGATOR_DOMAINS = {
    "arbeitnow.com",
    "www.arbeitnow.com",
    "remotive.com",
    "www.remotive.com",
    "remoteok.com",
    "www.remoteok.com",
    "themuse.com",
    "www.themuse.com",
    "weworkremotely.com",
    "www.weworkremotely.com",
    "jobicy.com",
    "www.jobicy.com",
    "remoteineurope.com",
    "www.remoteineurope.com",
    "euroremotejobs.com",
    "www.euroremotejobs.com",
    "remote.co",
    "www.remote.co",
    "himalayas.app",
    "www.himalayas.app",
    "workingnomads.com",
    "www.workingnomads.com",
    "remotifyeurope.com",
    "www.remotifyeurope.com",
}
SHORTENER_DOMAINS = {
    "amz.run",
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "buff.ly",
    "lnkd.in",
}


def looks_like_company_jobs_url(url: str) -> bool:
    parsed = urlparse(url)
    path = parsed.path.lower()
    host = parsed.netloc.lower()
    if host in AGGREGATOR_DOMAINS or host in SHORTENER_DOMAINS:
        return False
    tokens = ("career", "careers", "job", "jobs", "hiring", "work-with-us")
    if any(token in path for token in tokens):
        return True
    return any(token in host for token in ("careers.", ".careers", "jobs.", ".jobs"))


def is_target_title(title: str, vocabulary: TitleVocabulary) -> bool:
    low = title.lower()
    if any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", low) for term in vocabulary.excluded_terms):
        return False
    return bool(find_terms(title, vocabulary.role_terms))


def iter_string_values(obj) -> list[str]:
    values: list[str] = []
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            stack.extend(current.values())
            continue
        if isinstance(current, list):
            stack.extend(current)
            continue
        if isinstance(current, str):
            values.append(current)
    return values


def extract_urls_from_text(text: str) -> list[str]:
    normalized = unescape(text).replace("\\/", "/")
    return re.findall(URL_RE, normalized)


def redirect_targets(url: str) -> list[str]:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    keys = (
        "url",
        "u",
        "target",
        "redirect",
        "redirect_url",
        "destination",
        "dest",
        "job_url",
        "apply_url",
        "link",
    )
    out: list[str] = []
    for key in keys:
        for val in params.get(key, []):
            clean = unquote(str(val).strip())
            if clean.startswith("http://") or clean.startswith("https://"):
                out.append(clean)
    return out


def candidate_urls_from_row(row: dict) -> list[str]:
    urls: list[str] = []
    explicit_keys = (
        "url",
        "job_url",
        "apply_url",
        "application_url",
        "apply_link",
        "source_url",
        "external_url",
        "redirect_url",
    )
    for key in explicit_keys:
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            urls.append(value.strip())

    for text in iter_string_values(row):
        if "http://" not in text and "https://" not in text:
            continue
        urls.extend(extract_urls_from_text(text))

    expanded: list[str] = []
    seen: set[str] = set()
    for item in urls:
        clean = item.strip().rstrip(".,)")
        if clean.startswith("http://") or clean.startswith("https://"):
            if clean not in seen:
                seen.add(clean)
                expanded.append(clean)
            for target in redirect_targets(clean):
                if target not in seen:
                    seen.add(target)
                    expanded.append(target)
    return expanded


def fetch_page_links(url: str, timeout: int, *, client: AtsHTTPClient) -> list[str]:
    """Links found on ``url``. ``client`` is required (ULTRON has a single network path).

    ``HttpClient.get_text`` returns ``(status, final_url, text)``; it carries no headers, so the
    HTML check relies on the body only. The network failures MARK III swallowed (any ``Exception``)
    are swallowed here too, except the request budget and a blocked host or vendor (nothing was requested, so
    the page is unchecked, not empty), which propagate.
    """
    try:
        status, final_url, text = client.get_text(url)  # type: ignore[attr-defined]
    except (RequestBudgetExceeded, HostBlocked):
        raise
    except Exception:  # pragma: no cover - network/runtime dependent
        return []
    if status >= 400:
        return []

    out: list[str] = []
    seen: set[str] = set()

    def add(link: str) -> None:
        clean = link.strip().rstrip(".,)")
        if not clean or clean in seen:
            return
        if clean.startswith("http://") or clean.startswith("https://"):
            seen.add(clean)
            out.append(clean)
            for target in redirect_targets(clean):
                if target not in seen:
                    seen.add(target)
                    out.append(target)

    add(url)
    add(final_url)

    text = text or ""
    if "<html" not in text.lower() and "<a " not in text.lower():
        return out

    try:
        soup = BeautifulSoup(text, "html.parser")
        for node in soup.select("a[href]"):
            href = str(node.get("href", "")).strip()
            if href.startswith("http://") or href.startswith("https://"):
                add(href)
        for node in soup.select("meta[content]"):
            content = str(node.get("content", ""))
            for extracted in extract_urls_from_text(content):
                add(extracted)
    except Exception:
        # Malformed markup only skips the HTML pass; the plain-text URL scan below still runs.
        pass

    for extracted in extract_urls_from_text(text[:180000]):
        add(extracted)
    return out[:300]
