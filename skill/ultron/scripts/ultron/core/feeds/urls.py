"""URL extraction and redirect-unwrapping helpers for public job feeds."""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup

from ultron.core.ats.clients.base import AtsHTTPClient
from ultron.core.ats.registry import is_direct_supported_ats_host
from ultron.infra.net import HostBlocked, RequestBudgetExceeded

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
    # Job boards and listing sites whose pages copy employers' postings (seen in agent searches).
    "web3.career",
    "www.web3.career",
    "startup.jobs",
    "www.startup.jobs",
    "shecodes.io",
    "www.shecodes.io",
    "cvmarket.lt",
    "www.cvmarket.lt",
    "djinni.co",
    "www.djinni.co",
    "freehire.me",
    "www.freehire.me",
    "join.com",
    "www.join.com",
    "wellfound.com",
    "www.wellfound.com",
    "builtin.com",
    "www.builtin.com",
    "remoterocketship.com",
    "www.remoterocketship.com",
    "arc.dev",
    "www.arc.dev",
    "jobgether.com",
    "www.jobgether.com",
    "seedtable.com",
    "www.seedtable.com",
    "remotefront.com",
    "www.remotefront.com",
    "dynamitejobs.com",
    "www.dynamitejobs.com",
    "nodesk.co",
    "www.nodesk.co",
    "euremotejobs.com",
    "www.euremotejobs.com",
    "agenticjobs.com",
    "www.agenticjobs.com",
    # Venture-capital portfolio job boards (jobs.<fund>.com, mostly on Getro): copies of portfolio postings.
    "getro.com",
    "jobs.generalcatalyst.com",
    "jobs.a16z.com",
    "jobs.sequoiacap.com",
    "jobs.indexventures.com",
    "jobs.lsvp.com",
    "jobs.accel.com",
    "jobs.balderton.com",
    "jobs.northzone.com",
    "jobs.creandum.com",
    "jobs.atomico.com",
    "jobs.eqtventures.com",
    # Keyed role sources of ``ultron search``: their links are listing pages (adzuna.es, adzuna.co.uk, ...).
    "adzuna.com",
    "www.adzuna.com",
    "reed.co.uk",
    "www.reed.co.uk",
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
    """Return True if ``url`` appears to point to a company careers or job page."""
    parsed = urlparse(url)
    path = parsed.path.lower()
    host = parsed.netloc.lower()
    if host in AGGREGATOR_DOMAINS or host in SHORTENER_DOMAINS:
        return False
    tokens = ("career", "careers", "job", "jobs", "hiring", "work-with-us")
    if any(token in path for token in tokens):
        return True
    return any(token in host for token in ("careers.", ".careers", "jobs.", ".jobs"))


def iter_string_values(obj) -> list[str]:
    """Collect all string values recursively from nested mappings and lists."""
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
    """Find HTTP(S) URLs inside raw or HTML-escaped text."""
    normalized = unescape(text).replace("\\/", "/")
    return re.findall(URL_RE, normalized)


def redirect_targets(url: str) -> list[str]:
    """Extract destination URLs from known redirect query parameters."""
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
    """Extract candidate destination URLs from one structured feed row."""
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


# Second-level labels under a country domain (``arbeitnow.co.uk``, ``foo.com.au``).
COUNTRY_SLDS = frozenset({"co", "com", "org", "net"})
_AGGREGATORS = {d.removeprefix("www.") for d in AGGREGATOR_DOMAINS}
# Names too generic to match across TLDs (``remote.co`` must not claim ``remote.de`` or ``remote.acme.de``).
_GENERIC_AGGREGATOR_NAMES = frozenset({"remote"})
_AGGREGATOR_NAMES = {d.split(".")[0] for d in _AGGREGATORS} - _GENERIC_AGGREGATOR_NAMES


def is_aggregator_host(host: str) -> bool:
    """``host`` (a hostname or netloc) belongs to a job aggregator of ``AGGREGATOR_DOMAINS``.

    Also matches subdomains and the same site under another country domain (``www.arbeitnow.co.uk``
    for ``arbeitnow.com``): ``<name>.<tld>`` or ``<name>.<co|com|org|net>.<tld>`` whose name is a
    non-generic aggregator name. Hosts on a supported ATS domain are never aggregators.
    """
    h = str(host or "").lower().rpartition("@")[2]
    h = h.rsplit(":", 1)[0] if h.count(":") == 1 else h  # drop the port (IPv6 literals are not aggregators)
    h = h.rstrip(".").removeprefix("www.")
    if not h or is_direct_supported_ats_host(h):
        return False
    if h in _AGGREGATORS or any(h.endswith("." + d) for d in _AGGREGATORS):
        return True
    name, _, rest = h.partition(".")
    if not rest or name not in _AGGREGATOR_NAMES:
        return False
    labels = rest.split(".")
    return len(labels) == 1 or (len(labels) == 2 and labels[0] in COUNTRY_SLDS)
