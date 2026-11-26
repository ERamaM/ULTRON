"""Gather inspectable candidates from web search, watch, boards, and public feeds."""

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qsl, unquote, urlsplit

from ultron.core import presets
from ultron.core.discovery.url_filter import normalize_url
from ultron.core.feeds.urls import looks_like_company_jobs_url
from ultron.core.ranker.lexical import LexicalRanker
from ultron.core.web_search import searxng
from ultron.core.web_search.cascade import provider_mode, search_web
from ultron.core.web_search.dedup import deduplicate_hits
from ultron.core.web_search.models import WebSearchResult
from ultron.core.web_search.query_builder import build_queries, relaxed_queries
from ultron.infra.net import HostGate
from ultron.infra.searxng import prepare_search
from ultron.stages.candidates import candidate_identity
from ultron.stages.http_factory import load_vendor_budget
from ultron.stages.role_feeds import known_resolutions, query_terms, remember_resolutions
from ultron.stages.scout_finds import memory_flags
from ultron.stages.scout_resolve import classify_find
from ultron.toolkit.aggregators import aggregator_company, lookup_query, matches_lookup, title_company
from ultron.toolkit.inspect import (
    InspectionSession,
    board_aliases,
    cache_postings,
    cached_postings,
    inspection_candidate,
    run_context,
)
from ultron.toolkit.search import (
    DEFAULT_LIMIT,
    discover_sources,
    flag_memory,
    listed_cache_row,
    mark_watch_served,
    profile_for_query,
)

MAX_LIMIT = 100
DEFAULT_QUERIES = 20
MAX_QUERIES = 40
MAX_INSPECTIONS = 120
MAX_COMPANY_PROBES = 20
MAX_LOOKUPS = 8
DISCOVERY_SECONDS = 28.0
TOTAL_SECONDS = 60.0
MAX_AGGREGATOR_TARGETS = 12
MAX_AGGREGATOR_HOST_TARGETS = 3
_SOURCE_ORDER = {"ats_posting": 0, "listed": 1, "employer_page": 2, "aggregator": 3}
_NOISE_HOSTS = frozenset({
    "linkedin.com", "facebook.com", "instagram.com", "youtube.com", "twitter.com", "x.com", "indeed.com",
    "glassdoor.com", "glassdoor.es", "ziprecruiter.com", "jooble.org", "monster.com", "google.com",
    "wikipedia.org", "wiktionary.org", "dictionary.com", "dictionary.cambridge.org", "collinsdictionary.com",
    "rae.es", "fundeu.es", "payscale.com",
})
_NOISE_PATHS = frozenset({"login", "signin", "sign-in", "privacy", "register", "signup", "search", "auth", "oauth"})


_CAREER_INDEXES = frozenset({
    "career", "careers", "job", "jobs", "search", "job-search", "vacancies", "positions", "openings",
    "opportunities", "all-jobs", "search-results", "emploi", "empleo", "vacantes", "ofertas", "trabaja-con-nosotros",
})
# Verified search-result routes on recruiting portals that also publish individual postings.
_PORTAL_INDEX_PREFIXES = {"michaelpage.es": "/jobs/", "spainjobs.io": "/es/empleos/t/"}
_EDITORIAL_PATHS = frozenset({"news", "blog", "articles", "press", "noticias", "actualidad"})
_LOCAL_CAREER_PATHS = frozenset({"empleo", "trabajo", "vacantes", "emploi", "stellenangebote"})
_POSTING_KEYS = frozenset({"jobid", "job_id", "job", "gh_jid", "req", "requisitionid", "postingid", "vacancyid"})


def _blocked(memo: str, *, new_only: bool) -> bool:
    return memo in {"rejected", "applied"} or (new_only and memo != "new")


def _identity(url: str, aliases: dict[str, str] | None = None) -> str:
    return candidate_identity(inspection_candidate(url, "web_search", aliases or {}).model_dump())


def _classify(url: str, *, web_result: bool = False) -> str:
    kind = classify_find(url).outcome
    if kind == "rejected":
        return kind
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")
    segments = unquote(parts.path).lower().strip("/").split("/")
    if any(host == blocked or host.endswith("." + blocked) for blocked in _NOISE_HOSTS):
        return "rejected"
    if any(segment in _NOISE_PATHS for segment in segments):
        return "rejected"
    if web_result and kind == "employer_page":
        career_url = (looks_like_company_jobs_url(url) or bool(_LOCAL_CAREER_PATHS.intersection(segments))
                      or host.split(".")[0] in _LOCAL_CAREER_PATHS)
        posting_key = any(key.casefold() in _POSTING_KEYS and value for key, value in parse_qsl(parts.query))
        index = not segments[0] or segments[-1] in _CAREER_INDEXES
        prefix = _PORTAL_INDEX_PREFIXES.get(host.removeprefix("www."))
        if (prefix and parts.path.lower().startswith(prefix)) or _EDITORIAL_PATHS.intersection(segments):
            return "rejected"
        return kind if career_url and (not index or posting_key) else "rejected"
    return kind if kind == "ats_posting" or parts.path.strip("/") else "rejected"


def _web_candidates(web: WebSearchResult) -> list[dict]:
    return [{"url": hit.url, "title": hit.title, "snippet": hit.snippet,
             "company": aggregator_company(hit.url) or title_company(hit.title), "source": "web",
             "status": "unread"} for hit in web.hits if _classify(hit.url, web_result=True) != "rejected"]


def _candidate_pool(local: list[dict], web: WebSearchResult, aliases: dict[str, str]) -> tuple[list[dict], list[dict]]:
    open_rows, inspect = {}, {}
    for row in [*local, *_web_candidates(web)]:
        url = normalize_url(row["url"])
        if (kind := _classify(url)) == "rejected":
            continue
        identity = _identity(url, aliases) if url in aliases or "id" not in row else row["id"]
        if row["status"] == "open" and kind != "aggregator":
            open_rows.setdefault(identity, {**row, "id": identity, "url": url})
            continue
        item = {**row, "id": identity, "url": url, "kind": kind}
        if url not in inspect:
            inspect[url] = item
        elif row["status"] == "unread":
            current = inspect[url]
            current["company"] = current.get("company") or item["company"]
            current["snippet"] = item["snippet"]
    for row in inspect.values():
        if row["source"] == "watch" and row["id"] in open_rows:
            open_rows[row["id"]]["source"] = "watch"
    return list(open_rows.values()), [row for row in inspect.values() if row["id"] not in open_rows]


def _inspection_order(row: dict) -> int:
    if row["kind"] == "aggregator":
        return _SOURCE_ORDER["aggregator"]
    return _SOURCE_ORDER["listed" if row["status"] == "listed" and row["kind"] != "ats_posting" else row["kind"]]


def _target_plan(rows: list[dict], ranker: LexicalRanker, limit: int, known: dict[str, str]) -> list[dict]:
    # A cached employer URL counts as a direct candidate. Only uncached aggregators spend the aggregator allowance.
    prepared = []
    for row in rows:
        target = known.get(row["url"]) or row["url"]
        kind = row["kind"] if target == row["url"] else _classify(target)
        if kind in _SOURCE_ORDER:
            prepared.append({**row, "target": target, "kind": kind})
    ordered = sorted(ranker.ordered(prepared), key=_inspection_order)
    unique, identities = {}, set()
    host_counts: Counter[str] = Counter()
    aggregator_count = 0
    for row in ordered:
        target = row["target"]
        if target in unique or row["id"] in identities:
            continue
        if row["kind"] == "aggregator":
            host = (urlsplit(target).hostname or "").removeprefix("www.")
            if aggregator_count >= MAX_AGGREGATOR_TARGETS or host_counts[host] >= MAX_AGGREGATOR_HOST_TARGETS:
                continue
            host_counts[host] += 1
            aggregator_count += 1
        unique[target] = row
        identities.add(row["id"])
        if len(unique) >= limit:
            break
    return list(unique.values())

def _lookup_targets(unresolved: list[dict], original: dict[str, dict], ranker: LexicalRanker,
                    deadline: float) -> dict[str, dict]:
    query_rows = {}
    for dossier in ranker.ordered([original[row["url"]] for row in unresolved if row["url"] in original]):
        query = lookup_query(dossier.get("company") or "", dossier.get("title") or "")
        if query:
            query_rows.setdefault(query, dossier)
        if len(query_rows) >= MAX_LOOKUPS:
            break
    if not query_rows:
        return {}
    hits, _ = searxng.search(list(query_rows), deadline=deadline)
    variants = {variant: row for query, row in query_rows.items() for variant in (query, *relaxed_queries(query))}
    targets = {}
    have_listing = set()
    for hit in deduplicate_hits(hits):
        row = variants.get(hit.query)
        if row and row["url"] not in have_listing and matches_lookup(hit.url, hit.title, row["company"], row["title"]):
            targets[hit.url] = row
            have_listing.add(row["url"])
    return targets


def _canonical(row: dict) -> bool:
    return all(_classify(url) not in {"aggregator", "rejected"}
               for url in (row["url"], row.get("apply_url") or row["url"]))


def _inherit_sources(rows: list[dict], by_url: dict[str, dict]) -> None:
    for row in rows:
        origin = by_url.get(row.get("found_at")) or by_url.get(row["url"])
        if origin is not None:
            row["source"] = origin["source"]
            if origin["url"] != row["url"] and not row.get("found_at"):
                row["found_at"] = origin["url"]


def _persist(profile: str, returned: list[dict], caches: list[dict], boards: set[str]) -> None:
    cache = {(row["identity_key"], row["status"], row["url"]): row for row in caches}
    rows = []
    for dossier in returned:
        key = (dossier["id"], dossier["status"], dossier["url"])
        row = cache.get(key)
        rows.append(row if row is not None else listed_cache_row(dossier, boards))
    cache_postings(profile, rows)


def _mark_gather_watch(profile: str, returned: list[dict], candidates: list[dict]) -> None:
    returned_urls = {row.get("found_at") or row["url"] for row in returned if row.get("source") == "watch"}
    originals = [row for row in candidates if row["source"] == "watch" and normalize_url(row["url"]) in returned_urls]
    mark_watch_served(profile, originals, origin="gather")


def _inspect_lookups(
    inspector: InspectionSession, targets: dict[str, dict], rows: list[dict], caches: list[dict],
) -> tuple[int, set[str]]:
    if not targets:
        return 0, set()
    result = inspector.inspect([(url, row["company"]) for url, row in targets.items()])
    resolved = set()
    for row in result.dossiers:
        origin = targets.get(row.get("found_at") or row["url"])
        if (origin and row["status"] == "open" and _canonical(row)
                and matches_lookup(row["url"], row["title"], origin["company"], origin["title"])):
            row["found_at"] = origin["url"]
            row["source"] = origin["source"]
            row["via"] = "targeted_ats_search"
            inspector.resolutions[origin["url"]] = row["url"]
            resolved.add(origin["url"])
    rows.extend(result.dossiers)
    caches.extend(result.cache)
    return result.requests, resolved


def gather_candidates(profile_slug: str, query: str = "", *, limit: int = DEFAULT_LIMIT,
                      max_queries: int = DEFAULT_QUERIES, web_provider: str | None = None,
                      new_only: bool = False, detail: bool = False,
                      preset: str = presets.DEFAULT_PRESET) -> dict:
    """Return one batch of ranked factual dossiers and cache the selected postings for shortlist save."""
    if not 1 <= limit <= MAX_LIMIT or not 1 <= max_queries <= MAX_QUERIES:
        raise ValueError("limit must be 1-100 and max_queries must be 1-40")
    run = run_context(profile_slug, preset)
    profile = run["profile_snapshot"]
    terms = query_terms(profile, query)
    queries = build_queries(profile, max_queries, query=query if query.strip() else None)
    if query.strip():
        run = {**run, "profile_snapshot": profile_for_query(profile, terms)}
    mode = provider_mode(web_provider)
    docker_status = prepare_search() if queries and mode in {"auto", "searxng"} else {}
    start = time.monotonic()
    gate, budget = HostGate(), load_vendor_budget(run)
    discovery_deadline = start + DISCOVERY_SECONDS
    with ThreadPoolExecutor(max_workers=2) as pool:
        web_task = pool.submit(search_web, queries, mode, deadline=discovery_deadline)
        sources = discover_sources(run, terms, gate, budget, detail=detail, deadline=discovery_deadline)
        web = web_task.result()

    aliases = board_aliases(cached_postings(profile_slug))
    aliases.update(board_aliases({row["identity_key"]: row for row in sources.cache}))
    opened, candidates = _candidate_pool(sources.dossiers, web, aliases)
    flag_memory(profile_slug, candidates)
    eligible = [row for row in candidates if not _blocked(row["memory"], new_only=new_only)]
    skipped = len(candidates) - len(eligible)
    ranker = LexicalRanker(profile, query=query)
    searx_enabled = web.provider_used in {"searxng", "multi"} and any(hit.provider == "searxng" for hit in web.hits)
    targets = _target_plan(eligible, ranker, MAX_INSPECTIONS - (MAX_LOOKUPS if searx_enabled else 0), known_resolutions())

    def skip(key: str) -> bool:
        return _blocked(memory_flags(profile_slug, [key])[key], new_only=new_only)

    deadline = start + TOTAL_SECONDS
    inspector = InspectionSession(run, detail, aliases, gate=gate, vendor_budget=budget,
                                  skip_identity=skip, max_fallbacks=MAX_COMPANY_PROBES, deadline=deadline)
    origins = {row["target"]: row for row in targets}
    locations = [(url, row["company"]) for url, row in origins.items()]
    result = inspector.inspect(locations)
    rows = [*opened, *result.dossiers]
    caches = [*sources.cache, *result.cache]
    failed = [row for row in result.dossiers if row["status"] == "unresolved_aggregator"]
    original = {row["url"]: row for row in targets}
    lookup_deadline = min(deadline - 2.0, time.monotonic() + 5.0)
    followed = (_lookup_targets(failed, original, ranker, lookup_deadline)
                if searx_enabled and lookup_deadline > time.monotonic() else {})
    extra_requests, resolved_urls = _inspect_lookups(inspector, followed, rows, caches)
    failed = [row for row in failed if row["url"] not in resolved_urls]
    remember_resolutions(inspector.resolutions)
    _inherit_sources(rows, origins)
    verified = [row for row in rows if row["status"] in {"open", "listed"} and _canonical(row)]
    blocked = [row for row in rows if row["status"] in {"closed", "skipped_memory"}]
    blocked_ids = {row.get("id") for row in blocked}
    blocked_urls = {url for row in blocked for url in (row["url"], row.get("found_at")) if url}
    verified.extend(row for row in eligible if row["status"] == "listed" and row["id"] not in blocked_ids
                    and row["url"] not in blocked_urls and _canonical(row))
    flag_memory(profile_slug, verified)
    safe = [row for row in verified if not _blocked(row["memory"], new_only=new_only)]
    returned = [{key: value for key, value in row.items() if key not in {"snippet", "kind"}}
                for row in ranker.shortlist(safe, limit)]
    _persist(profile_slug, returned, caches, sources.boards)
    _mark_gather_watch(profile_slug, returned, sources.dossiers)

    counts = Counter(row["status"] for row in rows)
    metadata = {**web.metadata(), **docker_status}
    metadata["provider"] = metadata.pop("provider_used")
    metadata["unique_urls"] = metadata.pop("unique_hits")
    unresolved = [{"url": row["url"], "company": row.get("company") or original.get(row["url"], {}).get("company", "")}
                  for row in failed]
    inspected = {status: counts[status] for status in ("open", "listed", "closed", "failed", "unresolved_aggregator")}
    inspected["failed"] += counts["unusable"] + counts["rejected"]
    inspected["unresolved_aggregator"] = len(failed)
    inspected.update(attempted=len(locations) + len(followed), skipped_memory=skipped + counts["skipped_memory"],
                     requests=result.requests + extra_requests)
    return {"profile": profile_slug, "queries": queries, "web_search": metadata, "sources": sources.sources,
            "inspected": inspected, "returned": len(returned), "dossiers": returned, "unresolved": unresolved}
