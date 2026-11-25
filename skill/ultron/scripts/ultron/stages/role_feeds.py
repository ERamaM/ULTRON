"""Role-searchable job APIs and keyless feeds, queried with a local cache: shared by ``search`` and the watch.

Each source is asked once per role query and its rows are cached under ``<ULTRON_HOME>/toolkit/cache`` for the
source's ``ttl_hours``, which keeps ULTRON within each source's request guidance. Rows come back normalized
(``title``, ``company_name``, ``url``, ``location``, ``posted``, ``source``); API keys never reach error records.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable

from ultron.core.ats.clients import AtsClientError
from ultron.core.feeds.collectors import COLLECTORS
from ultron.core.feeds.role_search import SOURCES, RoleSource
from ultron.infra import home
from ultron.infra.net import HostBlocked, HttpError, RequestBudgetExceeded, URLRejected
from ultron.stages.feeds import load_sources

LOCAL_FEEDS = ("arbeitnow", "hn_who_is_hiring")
FEED_TTL_HOURS = 6.0
MAX_TERMS = 3
CACHE_DIR = ("toolkit", "cache")
NETWORK_ERRORS = (AtsClientError, HostBlocked, HttpError, URLRejected, RequestBudgetExceeded)
PAYLOAD_ERRORS = (ValueError, KeyError, TypeError, AttributeError)
_WORD_RE = re.compile(r"[a-z0-9+#&]+")


def query_terms(profile: dict, query: str = "") -> list[str]:
    """The role queries to send: the user's query, else the profile's first target titles (or focus terms)."""
    if query.strip():
        return [query.strip()[:100]]
    criteria = profile.get("criteria") or {}
    titles = [str(t).strip() for t in (criteria.get("target_titles") or []) if str(t).strip()]
    focus = [str(t).strip() for t in (criteria.get("role_focus_terms") or []) if str(t).strip()]
    return list(dict.fromkeys(titles or focus))[:MAX_TERMS]


# Seniority and filler words: role APIs match a query literally, so they are left out of what is sent.
_SENIORITY = frozenset({"senior", "sr", "junior", "jr", "lead", "staff", "principal", "mid", "head", "of", "the",
                        "and", "level", "ii", "iii", "iv"})


def api_terms(terms: list[str]) -> list[str]:
    """The role words of each term without seniority or filler ("Senior AI Engineer" -> "AI Engineer"), unique."""
    out = []
    for term in terms:
        words = [w for w in str(term).split() if w.casefold().strip(".,") not in _SENIORITY]
        if words:
            out.append(" ".join(words))
    return list(dict.fromkeys(out)) or list(terms)


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(str(text or "").casefold()))


def relevance(title: str, terms: list[str]) -> float:
    """How closely ``title`` matches the best of ``terms`` (share of a term's words in the title, 0 to 1)."""
    words = _words(title)
    scores = [len(words & wanted) / len(wanted) for term in terms if (wanted := _words(term))]
    return max(scores, default=0.0)


def _cache_path(name: str, key: str):
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return home.ultron_home().joinpath(*CACHE_DIR, f"{name}-{digest}.json")


class IncompleteFetch(Exception):
    """A source answered with errors: its rows (maybe partial) are used but never cached as a complete answer."""

    def __init__(self, rows: list[dict], errors: int) -> None:
        super().__init__(f"{errors} errors")
        self.rows, self.errors = rows, errors


def _fresh(path, ttl_hours: float) -> list[dict] | None:
    """The cached rows at ``path`` while younger than ``ttl_hours``; ``None`` when missing, stale or corrupt."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - float(data["fetched_at"]) < ttl_hours * 3600:
            return list(data["rows"])
    except (OSError, ValueError, KeyError, TypeError):
        return None  # a corrupt entry is fetched again
    return None


def cached_rows(name: str, key: str, ttl_hours: float, fetch: Callable[[], list[dict]]) -> tuple[list[dict], str]:
    """Rows from the cache while fresh (``cached``), else from ``fetch()`` (``ok``), which is then cached."""
    path = _cache_path(name, key)
    if (rows := _fresh(path, ttl_hours)) is not None:
        return rows, "cached"
    rows = fetch()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_at": time.time(), "rows": rows}, ensure_ascii=False), encoding="utf-8")
    return rows, "ok"


def role_source_rows(source: RoleSource, terms: list[str], client, timeout: int) -> tuple[dict, list]:
    """Ask one role API for every term: ``(status, rows)``; a keyed source without keys is ``not_configured``.

    A term that fails does not cost the others: their rows are kept and the source reports ``partial``.
    """
    requests = [(term, req) for term in api_terms(terms) if (req := source.build(term, "")) is not None]
    if not requests:
        return {"status": "not_configured" if source.keyed else "no_query"}, []
    rows, statuses, errors = [], set(), []
    for term, req in requests:
        def fetch(r=req) -> list[dict]:
            return source.parse(client.request_json(r.url, timeout=timeout, allowed_domains=(r.host,),
                                                    headers=r.headers))
        try:
            got, status = cached_rows(source.name, f"{req.url}|{term}", source.ttl_hours, fetch)
        except NETWORK_ERRORS as exc:
            errors.append(type(exc).__name__)
            continue
        except PAYLOAD_ERRORS as exc:
            errors.append(f"unexpected_payload: {type(exc).__name__}")
            continue
        rows.extend(got)
        statuses.add(status)
    if errors:
        return {"status": "partial" if statuses else "failed", "error": errors[0]}, rows
    return {"status": "cached" if statuses == {"cached"} else "ok"}, rows


def local_feed_rows(source: dict, client, timeout: int) -> tuple[dict, list]:
    """One keyless feed with no role query, read whole: ``(status, rows)``; the caller filters the rows."""
    def fetch() -> list[dict]:
        rows, errors = COLLECTORS[source["parser"]](client, source, timeout)
        clean = [{"title": r.get("headline") or r.get("title", ""), "company_name": r.get("company_name", ""),
                  "url": r.get("url", ""), "location": r.get("location") or r.get("headline", ""), "posted": "",
                  "description": str(r.get("description") or "")[:2000], "source": source["id"]}
                 for r in rows if r.get("url")]
        if errors:  # the collectors report failed pages instead of raising
            raise IncompleteFetch(clean, len(errors))
        return clean
    try:
        rows, status = cached_rows(source["id"], source["url"], FEED_TTL_HOURS, fetch)
    except IncompleteFetch as exc:
        return {"status": "partial" if exc.rows else "failed", "error": "feed_errors"}, exc.rows
    except NETWORK_ERRORS as exc:
        return {"status": "failed", "error": type(exc).__name__}, []
    except PAYLOAD_ERRORS as exc:
        return {"status": "failed", "error": f"unexpected_payload: {type(exc).__name__}"}, []
    return {"status": status}, rows


RESOLUTION_FILE = "aggregator_links.json"
RESOLUTION_TTL_DAYS = 14


def _resolution_path():
    return home.ultron_home().joinpath(*CACHE_DIR, RESOLUTION_FILE)


def known_resolutions() -> dict[str, str]:
    """Aggregator listing URLs already followed, mapped to their employer posting URL (``""``: no link found)."""
    path = _resolution_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    cutoff = time.time() - RESOLUTION_TTL_DAYS * 86400
    return {url: item["to"] for url, item in data.items()
            if isinstance(item, dict) and float(item.get("at", 0)) >= cutoff and isinstance(item.get("to"), str)}


def remember_resolutions(found: dict[str, str]) -> None:
    """Record aggregator listing URL -> employer posting URL (``""`` when the page links to none)."""
    if not found:
        return
    path = _resolution_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    now = time.time()
    data.update({url: {"to": target, "at": now} for url, target in found.items()})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def local_feeds() -> list[dict]:
    """The bundled keyless feeds ``search`` and the watch filter locally."""
    return [source for source in load_sources() if source["id"] in LOCAL_FEEDS]


def keyless_sources() -> list[RoleSource]:
    """The role APIs that need no key (the watch uses only these)."""
    return [source for source in SOURCES if not source.keyed]
