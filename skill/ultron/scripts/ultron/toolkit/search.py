"""``ultron search``: postings for a profile from sources web search does not reach.

Three kinds of source, in this order:

1. postings the watch found since the last session (served once, then marked queued);
2. ATS boards: the profile's watchlist, or on the first day (empty watchlist) the seed inventory boards that fit
   the profile's sectors and region;
3. public job APIs that take a role query (``core.feeds.role_search``) and the keyless feeds that do not
   (Arbeitnow, Hacker News "Who is hiring"), filtered locally (``stages.role_feeds``).

Every posting is matched against the profile's titles and location rules, ranked by how closely its title fits,
de-duplicated by company and title and flagged against memory. A board whose API carries each posting's text
gives ``open`` dossiers, with factual excerpts; the rest are ``listed`` (open at their source, not read yet), with the
excerpt their source returned when it returned one. All sources run in parallel; a shared host gate keeps any
host to one request at a time.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from ultron.core import presets
from ultron.core.discovery.url_filter import normalize_url
from ultron.core.feeds.role_search import SOURCES, RoleSource
from ultron.infra import home
from ultron.infra.net import HostGate, VendorBudget
from ultron.stages.candidates import candidate_identity
from ultron.stages.discover import profile_seeds, seed_key
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, client_for_run, load_vendor_budget
from ultron.stages.role_feeds import (
    known_resolutions,
    local_feed_rows,
    local_feeds,
    query_terms,
    relevance,
    role_source_rows,
)
from ultron.stages.scout_finds import memory_flags, web_candidate
from ultron.stages.watch import as_postings, profile_matches
from ultron.stages.watch_boards import watch_seeds
from ultron.storage import ledger, watchlist
from ultron.toolkit.boards import NETWORK_ERRORS, PAYLOAD_ERRORS, board_dossiers, list_board
from ultron.toolkit.dossier import ProfileContext, employer_name, geo_excerpt, role_excerpt
from ultron.toolkit.inspect import cache_postings, run_context

DEFAULT_LIMIT = 60
WORKERS = 8
DAY_ONE_BOARDS = 40


def listed(identity: str, row: dict, source: str) -> dict[str, Any]:
    """The dossier of a posting open at its source and not read yet, with the excerpt its source returned."""
    dossier: dict[str, Any] = {"id": identity, "url": row["url"], "status": "listed",
                               "title": str(row.get("title") or "")[:200],
                               "company": employer_name(row.get("company_name", ""), row["url"]),
                               "location": str(row.get("location") or "")[:200], "source": source}
    if row.get("posted"):
        dossier["posted"] = str(row["posted"])[:10]
    if text := row.get("description"):
        dossier["geo_excerpt"], dossier["role_excerpt"] = geo_excerpt(text), role_excerpt(text, detail=False)
    return dossier


def matching_rows(rows: list[dict], profile: dict, label: str) -> list[dict]:
    """Dossiers of the feed ``rows`` that match the profile's titles and location rules."""
    by_url = {r["url"]: r for r in rows}
    out = []
    for posting in profile_matches(as_postings(rows), profile):
        row = by_url[posting.apply_url]
        candidate = web_candidate(posting.apply_url, {"title": posting.title, "company": row.get("company_name", ""),
                                                      "location": posting.location}, f"search:{label}")
        out.append(listed(candidate_identity(candidate.model_dump()), row, label))
    return out


class _Searcher:
    """Queries each source with its own client; all clients share one host gate and one vendor budget."""

    def __init__(self, run: dict, terms: list[str], gate: HostGate, vendor_budget: VendorBudget | None,
                 *, detail: bool = False, deadline: float | None = None) -> None:
        self.run, self.terms = run, terms
        self.profile = run["profile_snapshot"]
        self.context = ProfileContext(self.profile)
        self.timeout = run["settings"]["ats_api_timeout"]
        self.gate, self.vendor_budget = gate, vendor_budget
        self.detail, self.deadline = detail, deadline

    def _client(self):
        return client_for_run(self.run, block_threshold=HOST_BLOCK_THRESHOLD, gate=self.gate,
                              vendor_budget=self.vendor_budget, deadline=self.deadline)

    def role_source(self, source: RoleSource) -> tuple[str, dict, list[dict], list[dict]]:
        """One role API; ``(name, status, dossiers)``."""
        with self._client() as client:
            status, rows = role_source_rows(source, self.terms, client, self.timeout)
        found = matching_rows(rows, self.profile, source.label)
        return source.name, {**status, "results": len(found)} if rows else status, found, []

    def local_feed(self, source: dict) -> tuple[str, dict, list[dict], list[dict]]:
        """One keyless feed, filtered by the profile and the role terms; ``(name, status, dossiers)``."""
        with self._client() as client:
            status, rows = local_feed_rows(source, client, self.timeout)
        found = [d for d in matching_rows(rows, self.profile, source["id"]) if relevance(d["title"], self.terms) > 0]
        return source["id"], {**status, "results": len(found)}, found, []

    def board(self, seed) -> tuple[str, dict, list[dict], list[dict]]:
        """One ATS board's postings that match the profile; ``(seed key, status, dossiers)``."""
        try:
            with self._client() as client:
                postings = list_board(seed, client, self.timeout)
        except NETWORK_ERRORS as exc:
            return seed_key(seed), {"status": "failed", "error": type(exc).__name__}, [], []
        except PAYLOAD_ERRORS as exc:
            return seed_key(seed), {"status": "failed", "error": f"unexpected_payload: {type(exc).__name__}"}, [], []
        dossiers, cache = board_dossiers(seed, postings, self.context, detail=self.detail, limit=None)  # ranked globally later
        dossiers = [d for d in dossiers if d["status"] != "closed"]
        for dossier in dossiers:
            dossier["source"] = seed_key(seed)
        return seed_key(seed), {"status": "ok", "results": len(dossiers)}, dossiers, cache


def _ledger_ready() -> bool:
    return (home.ultron_home() / "ultron.sqlite3").exists()


def _pending(profile_slug: str, profile: dict) -> list[dict]:
    """Postings the watch recorded that match ``profile`` (the query's role when one is asked).

    A board posting keeps its board. Postings that do not match stay pending for a later, relevant search.
    """
    if not _ledger_ready():
        return []
    with closing(ledger.connect()) as conn:
        rows = watchlist.pending(conn, profile_slug)
    by_url = {row.get("normalized_url", ""): row for row in rows}
    postings = as_postings([{"title": row.get("ats_title", ""), "url": url, "location": row.get("ats_location", ""),
                             "remote": row.get("ats_remote_hint", "")} for url, row in by_url.items() if url])
    out = []
    for row in (by_url[posting.apply_url] for posting in profile_matches(postings, profile)):
        url = row.get("normalized_url", "")
        dossier = {"id": row.get("identity_key") or candidate_identity(row), "url": url,
                   "status": "listed", "title": row.get("ats_title", ""),
                   "company": employer_name(row.get("ats_company", ""), url),
                   "location": row.get("ats_location", ""), "source": "watch"}
        if row.get("source_type") == "ats_api" and row.get("source_key"):
            dossier["board"] = row["source_key"]
        out.append(dossier)
    return out


def mark_watch_served(profile_slug: str, dossiers: list[dict], *, origin: str = "search") -> None:
    """Mark as queued only the watch postings this answer returns; the rest stay pending."""
    keys = [d["id"] for d in dossiers if d["source"] == "watch"]
    if keys and _ledger_ready():
        with closing(ledger.connect()) as conn:
            watchlist.mark_queued(conn, profile_slug, keys, origin)


def profile_for_query(profile: dict, terms: list[str]) -> dict:
    """``profile`` with its role titles and focus terms replaced by an explicit role query."""
    criteria = dict(profile.get("criteria") or {})
    criteria.update(target_titles=list(terms), role_focus_terms=list(terms), role_family_label=terms[0])
    # The profile's role vocabulary (packs, specialties, role signals) would match its usual role again.
    for key in ("must_have_terms", "vocabulary_packs", "specialties", "role_signal_terms"):
        criteria.pop(key, None)
    return {**profile, "criteria": criteria}


def listed_cache_row(dossier: dict, boards: set[str]) -> dict:
    """The inspect-cache row of a returned posting, so ``shortlist save`` can name it by ``id`` alone."""
    board = dossier.get("board") or (dossier["source"] if dossier["source"] in boards else "")
    job = {"apply_url": dossier["url"], "title": dossier["title"], "company": dossier["company"],
           "location": dossier["location"]}
    return {"identity_key": dossier["id"], "status": dossier["status"], "url": normalize_url(dossier["url"]),
            "source_type": "ats_api" if board else "web_search", "source_key": board, "job": job}


def flag_memory(profile_slug: str, dossiers: list[dict]) -> None:
    """Memory flags; an aggregator listing ULTRON already followed takes its employer posting's flag."""
    links = known_resolutions()
    resolved = {}
    for dossier in dossiers:
        if target := links.get(dossier["url"]):
            resolved[dossier["id"]] = candidate_identity(web_candidate(target, {}, "search").model_dump())
    flags = memory_flags(profile_slug, [*(d["id"] for d in dossiers), *resolved.values()])
    for dossier in dossiers:
        own = flags[dossier["id"]]
        employer = flags.get(resolved.get(dossier["id"], ""), "new")
        dossier["memory"] = employer if own == "new" or employer in {"applied", "rejected"} else own


def _rank(dossiers: list[dict], terms: list[str]) -> list[dict]:
    """Watch postings first, then read postings, then by title relevance; one dossier per posting and per role.

    The same role posted on several sources (company and title alike) is kept once, preferring a read posting.
    """
    ordered = sorted(dossiers, key=lambda d: (d["source"] != "watch", d["status"] != "open",
                                              -relevance(d["title"], terms)))
    seen_ids: set[str] = set()
    seen_roles: set[tuple[str, str]] = set()
    out = []
    for dossier in ordered:
        role = (dossier["company"].casefold(), dossier["title"].casefold())
        if dossier["id"] in seen_ids or (role[0] and role in seen_roles):
            continue
        seen_ids.add(dossier["id"])
        seen_roles.add(role)
        out.append(dossier)
    return out


@dataclass(slots=True)
class SourceDiscovery:
    """Discovery results with provenance and the cache rows already read from boards."""

    dossiers: list[dict]
    cache: list[dict]
    sources: dict[str, dict]
    boards: set[str]


def discover_sources(run: dict, terms: list[str], gate: HostGate, vendor_budget: VendorBudget | None, *,
                     detail: bool = False, deadline: float | None = None) -> SourceDiscovery:
    """Discover pending watch postings, ATS boards and feeds with shared network budgets."""
    searcher = _Searcher(run, terms, gate, vendor_budget, detail=detail, deadline=deadline)
    profile_slug = run["profile_slug"]
    seeds = watch_seeds(run) or profile_seeds(profile_slug, run["profile_snapshot"], DAY_ONE_BOARDS,
                                              run["settings"]["max_requests_per_host"])
    boards_to_query = [(searcher.board, seed) for seed in seeds]
    feeds_to_query = [(searcher.role_source, source) for source in SOURCES]
    feeds_to_query += [(searcher.local_feed, source) for source in local_feeds()]
    jobs = (boards_to_query + feeds_to_query if deadline is None else
            feeds_to_query + boards_to_query)
    sources: dict[str, dict] = {}
    dossiers, cache = [], []
    boards: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        pending = pool.submit(_pending, profile_slug, run["profile_snapshot"])
        discovered = pool.map(lambda job: job[0](job[1]), jobs)
        watched = pending.result()
        sources["watch"] = {"status": "ok", "results": len(watched)}
        dossiers.extend(watched)
        for name, status, found, rows in discovered:
            (boards if ":" in name else sources)[name] = status
            dossiers.extend(found)
            cache.extend(rows)
    sources["boards"] = {"queried": len(boards), "failed": sum(1 for s in boards.values() if s["status"] == "failed"),
                         "results": sum(s.get("results", 0) for s in boards.values())}
    return SourceDiscovery(dossiers, cache, sources, {seed_key(seed) for seed in seeds})


def search_jobs(profile_slug: str, query: str = "", *, new_only: bool = False, limit: int = DEFAULT_LIMIT,
                preset: str = presets.DEFAULT_PRESET) -> dict[str, Any]:
    """Postings for ``profile_slug`` from the watch, ATS boards and role-searchable job APIs.

    ``query`` replaces the profile's target titles as the role query. ``new_only`` keeps postings the profile has
    never seen. Each result has its ``source``; ``open`` ones were read, ``listed`` ones can be read with
    ``inspect``.
    """
    run = run_context(profile_slug, preset)
    terms = query_terms(run["profile_snapshot"], query)
    if query.strip():  # match titles against the asked role; location and eligibility rules stay the profile's
        run = {**run, "profile_snapshot": profile_for_query(run["profile_snapshot"], terms)}
    found = discover_sources(run, terms, HostGate(), load_vendor_budget(run))
    dossiers = found.dossiers
    # Memory first: with new_only, a seen posting must not hide a new opening of the same role.
    flag_memory(profile_slug, dossiers)
    if new_only:
        dossiers = [d for d in dossiers if d["memory"] == "new"]
    ranked = _rank(dossiers, terms)
    cache_postings(profile_slug, [listed_cache_row(d, found.boards) for d in ranked if d.get("url")])
    returned = ranked[:max(1, limit)]
    mark_watch_served(profile_slug, returned)
    return {"profile": profile_slug, "queries": terms, "sources": found.sources, "results": len(ranked),
            "dossiers": returned}
