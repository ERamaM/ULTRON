"""HireHive: a tenant's public board (``https://<tenant>.hirehive.com/``) lists every open job on one page.

One GET per board, no pagination. Job cards (``hh-job-row``) give the title, link and location; the schema.org
``ItemList`` JSON-LD on the same page is the fallback when the cards are missing. Only links on the tenant's own
host are kept, and at most ``MAX_POSTINGS`` of them.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import AtsClientError, AtsHTTPClient, AtsPosting, locator_url

MAX_POSTINGS = 500
_BOARD_MARKER = "hh-job-listings"
_TENANT_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", re.ASCII)


def _clean(text: str) -> str:
    return " ".join(str(text or "").split())


def _tenant_url(board_url: str, href: str, host: str) -> str:
    """Absolute posting URL without query or fragment, or ``""`` when the link leaves the tenant's host."""
    parts = urlsplit(urljoin(board_url, str(href or "").strip()))
    if parts.scheme not in {"http", "https"} or (parts.hostname or "").lower() != host or len(parts.path) <= 1:
        return ""
    return urlunsplit(("https", host, parts.path, "", ""))


def _card_listings(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for card in soup.select("a.hh-job-row[href]"):
        title_el = card.select_one('[class*="hh-job-row-title"]')
        location_el = card.select_one('[class*="hh-job-row-location"]')
        title = _clean(title_el.get_text(" ", strip=True)) if title_el is not None else ""
        location = _clean(location_el.get_text(" ", strip=True)) if location_el is not None else ""
        rows.append((title, str(card.get("href") or ""), location))
    return rows


def _item_list_listings(soup: BeautifulSoup) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except ValueError:
            continue
        if not isinstance(data, dict) or data.get("@type") != "ItemList":
            continue
        for item in data.get("itemListElement") or []:
            if isinstance(item, dict):
                rows.append((_clean(item.get("name")), str(item.get("url") or ""), ""))
    return rows


def fetch_hirehive_jobs(board_identifier: str, timeout: int = 20, *, client: AtsHTTPClient) -> list[AtsPosting]:
    """Fetch open postings from a HireHive hosted board."""
    tenant = str(board_identifier or "").strip().lower()
    if not _TENANT_RE.fullmatch(tenant):
        raise AtsClientError("invalid_board")  # the board is one DNS label: <tenant>.hirehive.com
    host = f"{tenant}.hirehive.com"
    board_url = locator_url(host) + "/"
    html = client.request_text(board_url, timeout=timeout)
    soup = BeautifulSoup(html or "", "html.parser")
    item_list = _item_list_listings(soup)
    if soup.select_one(f'[class*="{_BOARD_MARKER}"]') is None and not item_list:
        # Not a job board (an error or marketing page): never report it as a verified empty board.
        raise AtsClientError("unparseable_board")
    out: list[AtsPosting] = []
    seen: set[str] = set()
    for title, href, location in _card_listings(soup) or item_list:
        apply_url = _tenant_url(board_url, href, host)
        if not title or not apply_url or apply_url in seen:
            continue
        seen.add(apply_url)
        out.append(AtsPosting(title=title, apply_url=apply_url, location=location))
        if len(out) >= MAX_POSTINGS:
            break
    return out
