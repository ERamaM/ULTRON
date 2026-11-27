"""Feed collectors (literal port of MARK III ``sources/feeds.py`` ``collect_*``) and the source registry.

Deviations from the original: ``RequestBudgetExceeded`` is re-raised instead of being swallowed
into a discard (the stage needs it to honour the run budget); the HN search/items URLs and the
RemotifyEurope location blocklist come from ``feeds.yaml`` instead of being hardcoded; the
network callables are ``HttpClient.request_json`` / ``request_text``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from functools import partial
from urllib.parse import urlsplit

from ultron.core.feeds.parsers import (
    extract_company_entries_from_remote_es_markdown,
    extract_company_names_from_remotifyeurope_html,
    extract_company_names_from_weworkremotely_top_html,
    rows_from_himalayas_payload,
    rows_from_jobicy_api_payload,
    rows_from_remoteok_payload,
    rows_from_rss_payload,
    rows_from_themuse_payload,
)
from ultron.core.feeds.urls import extract_urls_from_text
from ultron.infra.net import RequestBudgetExceeded


def collect_workingnomads_urls(timeout: int, *, api_url: str, request_json) -> tuple[list[dict], list[dict]]:
    """Collect candidate job rows from the WorkingNomads JSON API."""
    try:
        payload = request_json(api_url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        return [], [{"url": api_url, "reason": "ats_seed_discovery_api_error", "detail": f"workingnomads:{exc}"}]
    if not isinstance(payload, list):
        return [], []
    rows: list[dict] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        company = str(item.get("company_name") or "").strip()
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        if not company and not url:
            continue
        row = {"title": title, "company_name": company, "url": url, "source": "workingnomads"}
        loc = str(item.get("location") or "").strip()
        if loc:
            row["location"] = loc
        rows.append(row)
    return rows, []


def collect_remotive_urls(timeout: int, *, api_url: str, request_json) -> tuple[list[dict], list[dict]]:
    """Collect candidate job rows from the Remotive JSON API."""
    try:
        payload = request_json(api_url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        return [], [{"url": api_url, "reason": "ats_seed_discovery_api_error", "detail": f"remotive:{exc}"}]
    jobs = []
    if isinstance(payload, dict):
        raw_jobs = payload.get("jobs", [])
        if isinstance(raw_jobs, list):
            jobs = [item for item in raw_jobs if isinstance(item, dict)]
    rows: list[dict] = []
    for item in jobs:
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        row = {
            "title": str(item.get("title") or "").strip(),
            "company_name": str(item.get("company_name") or "").strip(),
            "url": url,
            "source": "remotive",
        }
        description = str(item.get("description") or "").strip()
        if description:
            row["description"] = description
        loc = str(item.get("candidate_required_location") or item.get("location") or "").strip()
        if loc:
            row["location"] = loc
        rows.append(row)
    return rows, []


def collect_rss_urls(
    url: str, source: str, timeout: int, *, max_items: int, request_text
) -> tuple[list[dict], list[dict]]:
    """Collect up to ``max_items`` candidate job rows from an RSS/Atom feed URL."""
    try:
        payload = request_text(url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        return [], [{"url": url, "reason": "ats_seed_discovery_api_error", "detail": f"{source}:{exc}"}]
    rows = rows_from_rss_payload(payload, source=source)
    return rows[: max(1, max_items)], []


def collect_jobicy_api_urls(timeout: int, *, api_url: str, request_json) -> tuple[list[dict], list[dict]]:
    """Collect candidate job rows from the Jobicy public JSON API."""
    try:
        payload = request_json(f"{api_url}?count=50", timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        return [], [
            {
                "url": f"{api_url}?count=50",
                "reason": "ats_seed_discovery_api_error",
                "detail": f"jobicy_api:{exc}",
            }
        ]
    return rows_from_jobicy_api_payload(payload), []


def collect_remoteok_urls(timeout: int, *, api_url: str, request_json) -> tuple[list[dict], list[dict]]:
    """Collect candidate job rows from the RemoteOK JSON API."""
    try:
        payload = request_json(api_url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        return [], [{"url": api_url, "reason": "ats_seed_discovery_api_error", "detail": f"remoteok:{exc}"}]
    return rows_from_remoteok_payload(payload), []


def collect_themuse_urls(timeout: int, max_pages: int, *, api_url: str, request_json) -> tuple[list[dict], list[dict]]:
    """Collect paginated candidate job rows from The Muse API."""
    rows: list[dict] = []
    discards: list[dict] = []
    pages_cap = max(1, min(max_pages, 6))
    for page in range(1, pages_cap + 1):
        page_url = f"{api_url}?page={page}"
        try:
            payload = request_json(page_url, timeout=timeout)
        except RequestBudgetExceeded:
            raise
        except Exception as exc:
            discards.append(
                {
                    "url": page_url,
                    "reason": "ats_seed_discovery_api_error",
                    "detail": f"themuse:{exc}",
                }
            )
            break
        page_rows, page_count = rows_from_themuse_payload(payload)
        rows.extend(page_rows)
        if page_count is not None and page >= page_count:
            break
    return rows, discards


def collect_github_awesome_companies(timeout: int, *, url: str, request_text) -> tuple[list[dict], list[dict]]:
    """Collect company career-link hints from an Awesome Companies README."""
    rows: list[dict] = []
    discards: list[dict] = []
    try:
        text = request_text(url, timeout=timeout)
        for match in re.finditer(r"\|\s*\[([^\]]+)\]\((https?://[^)]+)\)", text):
            rows.append(
                {
                    "title": "Software Engineer",
                    "company_name": match.group(1).strip(),
                    "url": match.group(2).strip(),
                    "source": "github_awesome_remote",
                }
            )
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        discards.append({"url": url, "reason": "ats_seed_discovery_api_error", "detail": f"github_awesome:{exc}"})
    return rows[:200], discards


def collect_hn_who_is_hiring(
    timeout: int, *, max_comments: int, request_json, search_url: str, items_url: str
) -> tuple[list[dict], list[dict]]:
    """Collect company and job hints from HN Who Is Hiring threads."""
    try:
        search_data = request_json(search_url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        return [], [
            {"url": search_url, "reason": "ats_seed_discovery_api_error", "detail": f"hn_who_is_hiring_search:{exc}"}
        ]
    hits = search_data.get("hits", []) if isinstance(search_data, dict) else []
    if not hits or not hits[0].get("objectID"):
        return [], []
    item_url = f"{items_url.rstrip('/')}/{hits[0]['objectID']}"
    try:
        item_data = request_json(item_url, timeout=timeout)
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        return [], [
            {"url": item_url, "reason": "ats_seed_discovery_api_error", "detail": f"hn_who_is_hiring_items:{exc}"}
        ]
    rows: list[dict] = []
    children = item_data.get("children", []) if isinstance(item_data, dict) else []
    for child in children[:max_comments]:
        if not isinstance(child, dict):
            continue
        text = str(child.get("text") or "")
        if not text:
            continue
        first_line = text.split("\n")[0].split("<p>")[0]
        parts = [part.strip() for part in re.sub(r"<[^>]+>", " ", first_line).strip().split("|")]
        company_name = parts[0] if parts else ""
        urls = extract_urls_from_text(text)
        url = urls[0] if urls else ""
        if company_name or url:
            rows.append(
                {
                    "title": parts[2] if len(parts) > 2 else "",
                    "company_name": company_name,
                    "url": url,
                    "source": "hn_who_is_hiring",
                    # The whole first line names the role and the place ("Acme | ML Engineer | Remote EU").
                    "headline": " | ".join(part for part in parts if part),
                    # Truncated before stripping, with a bounded tag pattern: feed text is untrusted.
                    "description": " ".join(re.sub(r"<[^<>]{0,300}>", " ", text[:8000]).split())[:2000],
                }
            )
    return rows, []


def collect_weworkremotely_top_company_hints(
    timeout: int,
    *,
    urls: tuple[str, ...],
    max_companies: int,
    request_text,
) -> tuple[list[dict], list[dict]]:
    """Collect top company hints from WeWorkRemotely pages."""
    discards: list[dict] = []
    names: list[str] = []
    for url in urls:
        try:
            html = request_text(url, timeout=min(timeout, 12))
        except RequestBudgetExceeded:
            raise
        except Exception as exc:
            discards.append(
                {"url": url, "reason": "ats_seed_discovery_api_error", "detail": f"weworkremotely_top:{exc}"}
            )
            continue
        names = extract_company_names_from_weworkremotely_top_html(html)
        if names:
            break
    rows = [
        {"title": "Top remote company", "company_name": name, "source": "weworkremotely_top"}
        for name in names[: max(1, max_companies)]
    ]
    return rows, discards


def collect_remote_es_company_hints(timeout: int, *, url: str, request_text) -> tuple[list[dict], list[dict]]:
    """Collect company hints from the remote.es directory."""
    try:
        text = request_text(url, timeout=min(timeout, 14))
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        return [], [{"url": url, "reason": "ats_seed_discovery_api_error", "detail": f"remote_es_remotes:{exc}"}]
    rows = [
        {"title": "Remote company", "company_name": name, "source": "remote_es_remotes", "url": board_url}
        for name, board_url in extract_company_entries_from_remote_es_markdown(text)
    ]
    return rows, []


def collect_remotifyeurope_company_hints(
    timeout: int, *, url: str, request_text, location_names=()
) -> tuple[list[dict], list[dict]]:
    """Collect company hints from RemotifyEurope."""
    try:
        html = request_text(url, timeout=min(timeout, 14))
    except RequestBudgetExceeded:
        raise
    except Exception as exc:
        return [], [{"url": url, "reason": "ats_seed_discovery_api_error", "detail": f"remotifyeurope:{exc}"}]
    rows = [
        {"title": "RemotifyEurope company", "company_name": name, "source": "remotifyeurope"}
        for name in extract_company_names_from_remotifyeurope_html(html, location_names)
    ]
    return rows, []


def collect_arbeitnow_urls(
    timeout: int, max_pages: int, *, api_url: str, request_json
) -> tuple[list[dict], list[dict]]:
    """Collect paginated candidate job rows from the Arbeitnow API."""
    rows: list[dict] = []
    discards: list[dict] = []
    next_url = api_url
    pages = 0
    while next_url and pages < max_pages:
        pages += 1
        try:
            payload = request_json(next_url, timeout=timeout)
        except RequestBudgetExceeded:
            raise
        except Exception as exc:
            discards.append({"url": next_url, "reason": "ats_seed_discovery_api_error", "detail": f"arbeitnow:{exc}"})
            break
        if not isinstance(payload, dict):
            break
        data = payload.get("data", [])
        if isinstance(data, list):
            rows.extend(item for item in data if isinstance(item, dict))
        links = payload.get("links", {})
        next_url = str(links.get("next") or "").strip() if isinstance(links, dict) else ""
    return rows, discards


def collect_himalayas_urls(
    timeout: int, max_pages: int, *, api_url: str, request_json
) -> tuple[list[dict], list[dict]]:
    """Collect paginated candidate job rows from the Himalayas API."""
    rows: list[dict] = []
    discards: list[dict] = []
    limit = 50
    for page in range(max(1, max_pages)):
        page_url = f"{api_url}?limit={limit}&offset={page * limit}"
        try:
            payload = request_json(page_url, timeout=timeout)
        except RequestBudgetExceeded:
            raise
        except Exception as exc:
            discards.append({"url": page_url, "reason": "ats_seed_discovery_api_error", "detail": f"himalayas:{exc}"})
            break
        rows.extend(rows_from_himalayas_payload(payload))
        raw_jobs = payload.get("jobs") if isinstance(payload, dict) else payload
        if not isinstance(raw_jobs, list) or len(raw_jobs) < limit:
            break
    return rows, discards


def _c_arbeitnow(client, source, timeout):
    url = source["url"]
    host = urlsplit(url).hostname
    if not host:
        return [], [{"url": url, "reason": "ats_seed_discovery_api_error", "detail": "arbeitnow: missing feed host"}]
    request_json = partial(client.request_json, allowed_domains=(host,))
    return collect_arbeitnow_urls(timeout, int(source.get("max_pages", 3)), api_url=url, request_json=request_json)


def _c_himalayas(client, source, timeout):
    return collect_himalayas_urls(
        timeout, int(source.get("max_pages", 3)), api_url=source["url"], request_json=client.request_json
    )


def _c_remotive(client, source, timeout):
    return collect_remotive_urls(timeout, api_url=source["url"], request_json=client.request_json)


def _c_remoteok(client, source, timeout):
    return collect_remoteok_urls(timeout, api_url=source["url"], request_json=client.request_json)


def _c_themuse(client, source, timeout):
    return collect_themuse_urls(
        timeout, int(source.get("max_pages", 3)), api_url=source["url"], request_json=client.request_json
    )


def _c_rss(client, source, timeout):
    return collect_rss_urls(
        source["url"], source["id"], timeout, max_items=int(source.get("max_items", 350)),
        request_text=client.request_text,
    )


def _c_workingnomads(client, source, timeout):
    return collect_workingnomads_urls(timeout, api_url=source["url"], request_json=client.request_json)


def _c_jobicy_api(client, source, timeout):
    return collect_jobicy_api_urls(timeout, api_url=source["url"], request_json=client.request_json)


def _c_wwr_top(client, source, timeout):
    urls = tuple(source.get("urls") or [source["url"]])
    return collect_weworkremotely_top_company_hints(
        timeout, urls=urls, max_companies=int(source.get("max_companies", 100)), request_text=client.request_text
    )


def _c_remote_es(client, source, timeout):
    return collect_remote_es_company_hints(timeout, url=source["url"], request_text=client.request_text)


def _c_remotifyeurope(client, source, timeout):
    return collect_remotifyeurope_company_hints(
        timeout, url=source["url"], request_text=client.request_text,
        location_names=tuple(source.get("exclude_names") or ()),
    )


def _c_github_awesome(client, source, timeout):
    return collect_github_awesome_companies(timeout, url=source["url"], request_text=client.request_text)


def _c_hn(client, source, timeout):
    return collect_hn_who_is_hiring(
        timeout, max_comments=int(source.get("max_comments", 200)), request_json=client.request_json,
        search_url=source["url"], items_url=source["items_url"],
    )


# parser id (``feeds.yaml``) -> collector(client, source, timeout) -> (rows, discards)
COLLECTORS: dict[str, Callable] = {
    "arbeitnow": _c_arbeitnow,
    "himalayas": _c_himalayas,
    "remotive": _c_remotive,
    "remoteok": _c_remoteok,
    "themuse": _c_themuse,
    "rss": _c_rss,
    "workingnomads": _c_workingnomads,
    "jobicy_api": _c_jobicy_api,
    "weworkremotely_top": _c_wwr_top,
    "remote_es_remotes": _c_remote_es,
    "remotifyeurope": _c_remotifyeurope,
    "github_awesome": _c_github_awesome,
    "hn_who_is_hiring": _c_hn,
}
