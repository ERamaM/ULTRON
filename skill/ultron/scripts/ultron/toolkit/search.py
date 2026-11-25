"""``ultron search``: postings for a profile from sources web search does not reach.

Three kinds of source, in this order:

1. postings the watch found since the last session (served once, then marked queued);
2. the profile's watchlist boards, queried through the ATS clients;
3. public job APIs that take a role query (``core.feeds.role_search``) and the keyless feeds that do not
   (Arbeitnow, Hacker News "Who is hiring"), filtered locally (``stages.role_feeds``).

Every posting is matched against the profile's titles and location rules, ranked by how closely its title fits,
flagged against memory and returned as a ``listed`` dossier: open at its source, not read yet. ``inspect`` reads it.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from typing import Any

from ultron.core import presets
from ultron.core.ats.clients import fetch_vendor_jobs
from ultron.core.discovery.url_filter import normalize_url
from ultron.core.feeds.role_search import SOURCES, RoleSource
from ultron.infra import home
from ultron.stages.candidates import candidate_identity
from ultron.stages.discover import clean_posting, posting_candidate, seed_key
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, client_for_run
from ultron.stages.role_feeds import (
    NETWORK_ERRORS,
    PAYLOAD_ERRORS,
    local_feed_rows,
    local_feeds,
    query_terms,
    relevance,
    role_source_rows,
)
from ultron.stages.scout import watch_seeds
from ultron.stages.scout_finds import memory_flags, web_candidate
from ultron.stages.watch import as_postings, profile_matches
from ultron.storage import ledger, watchlist
from ultron.toolkit.inspect import cache_postings, run_context

DEFAULT_LIMIT = 60
WORKERS = 8


def listed(identity: str, posting, company: str, source: str) -> dict[str, Any]:
    """The dossier of a posting open at its source and not read yet."""
    dossier: dict[str, Any] = {"id": identity, "url": posting.apply_url, "status": "listed",
                               "title": str(posting.title or "")[:200], "company": str(company or "")[:200],
                               "location": str(posting.location or "")[:200], "source": source}
    if posting.posted_at:
        dossier["posted"] = str(posting.posted_at)[:10]
    return dossier


def matching_rows(rows: list[dict], profile: dict, label: str) -> list[dict]:
    """Dossiers of the feed ``rows`` that match the profile's titles and location rules."""
    company = {r["url"]: r.get("company_name", "") for r in rows}
    out = []
    for posting in profile_matches(as_postings(rows), profile):
        candidate = web_candidate(posting.apply_url, {"title": posting.title, "company": company[posting.apply_url],
                                                      "location": posting.location}, f"search:{label}")
        out.append(listed(candidate_identity(candidate.model_dump()), posting, company[posting.apply_url], label))
    return out


class _Searcher:
    """Queries each source with its own HTTP client (clients are not shared across threads)."""

    def __init__(self, run: dict, terms: list[str]) -> None:
        self.run, self.terms = run, terms
        self.profile = run["profile_snapshot"]
        self.timeout = run["settings"]["ats_api_timeout"]

    def _client(self):
        return client_for_run(self.run, block_threshold=HOST_BLOCK_THRESHOLD)

    def role_source(self, source: RoleSource) -> tuple[str, dict, list[dict]]:
        status, rows = role_source_rows(source, self.terms, self._client(), self.timeout)
        found = matching_rows(rows, self.profile, source.label)
        return source.name, {**status, "results": len(found)} if rows else status, found

    def local_feed(self, source: dict) -> tuple[str, dict, list[dict]]:
        status, rows = local_feed_rows(source, self._client(), self.timeout)
        found = [d for d in matching_rows(rows, self.profile, source["id"]) if relevance(d["title"], self.terms) > 0]
        return source["id"], {**status, "results": len(found)}, found

    def boards(self, seeds: list) -> list[tuple[str, dict, list[dict]]]:
        """Every watched board, serially with one client: boards of one vendor share its API host and budget."""
        client = self._client()
        return [self.board(seed, client) for seed in seeds]

    def board(self, seed, client) -> tuple[str, dict, list[dict]]:
        """One watched board's postings that match the profile; ``(seed key, status, dossiers)``."""
        try:
            postings = [clean_posting(p) for p in fetch_vendor_jobs(seed.ats, seed.board_identifier,
                                                                    client=client, timeout=self.timeout)]
        except NETWORK_ERRORS as exc:
            return seed_key(seed), {"status": "failed", "error": type(exc).__name__}, []
        except PAYLOAD_ERRORS as exc:
            return seed_key(seed), {"status": "failed", "error": f"unexpected_payload: {type(exc).__name__}"}, []
        out = []
        for posting in profile_matches(postings, self.profile):
            if (candidate := posting_candidate(seed, posting)) is not None:
                out.append(listed(candidate_identity(candidate.model_dump()), posting, seed.company, seed_key(seed)))
        return seed_key(seed), {"status": "ok", "results": len(out)}, out


def _ledger_ready() -> bool:
    return (home.ultron_home() / "ultron.sqlite3").exists()


def _pending(profile_slug: str) -> list[dict]:
    """Postings the watch recorded since the last session; a board posting keeps its board."""
    if not _ledger_ready():
        return []
    with closing(ledger.connect()) as conn:
        rows = watchlist.pending(conn, profile_slug)
    out = []
    for row in rows:
        dossier = {"id": row.get("identity_key") or candidate_identity(row), "url": row.get("normalized_url", ""),
                   "status": "listed", "title": row.get("ats_title", ""), "company": row.get("ats_company", ""),
                   "location": row.get("ats_location", ""), "source": "watch"}
        if row.get("source_type") == "ats_api" and row.get("source_key"):
            dossier["board"] = row["source_key"]
        out.append(dossier)
    return out


def _mark_served(profile_slug: str, dossiers: list[dict]) -> None:
    """Mark as queued only the watch postings this answer returns; the rest stay pending."""
    keys = [d["id"] for d in dossiers if d["source"] == "watch"]
    if keys and _ledger_ready():
        with closing(ledger.connect()) as conn:
            watchlist.mark_queued(conn, profile_slug, keys, "search")


def profile_for_query(profile: dict, terms: list[str]) -> dict:
    """``profile`` with its role titles and focus terms replaced by an explicit role query."""
    criteria = dict(profile.get("criteria") or {})
    criteria.update(target_titles=list(terms), role_focus_terms=list(terms), role_family_label=terms[0])
    criteria.pop("must_have_terms", None)
    return {**profile, "criteria": criteria}


def _cache_row(dossier: dict, boards: set[str]) -> dict:
    """The inspect-cache row of a listed posting, so ``shortlist save`` can name it by ``id`` alone."""
    board = dossier.get("board") or (dossier["source"] if dossier["source"] in boards else "")
    job = {"apply_url": dossier["url"], "title": dossier["title"], "company": dossier["company"],
           "location": dossier["location"]}
    return {"identity_key": dossier["id"], "status": "listed", "url": normalize_url(dossier["url"]),
            "source_type": "ats_api" if board else "web_search", "source_key": board, "job": job}


def _rank(dossiers: list[dict], terms: list[str]) -> list[dict]:
    """Watch postings first, then by title relevance; one dossier per posting."""
    unique: dict[str, dict] = {}
    for dossier in dossiers:
        unique.setdefault(dossier["id"], dossier)
    return sorted(unique.values(), key=lambda d: (d["source"] != "watch", -relevance(d["title"], terms)))


def search_jobs(profile_slug: str, query: str = "", *, new_only: bool = False, limit: int = DEFAULT_LIMIT,
                preset: str = presets.DEFAULT_PRESET) -> dict[str, Any]:
    """Postings for ``profile_slug`` from the watch, the watchlist boards and role-searchable job APIs.

    ``query`` replaces the profile's target titles as the role query. ``new_only`` keeps postings the profile has
    never seen. Each result is a ``listed`` dossier with its ``source``; read it with ``inspect``.
    """
    run = run_context(profile_slug, preset)
    terms = query_terms(run["profile_snapshot"], query)
    if query.strip():  # match titles against the asked role; location and eligibility rules stay the profile's
        run = {**run, "profile_snapshot": profile_for_query(run["profile_snapshot"], terms)}
    searcher = _Searcher(run, terms)
    # Each role source has its own host, so they run in parallel; all ATS boards run in one serial job.
    seeds = watch_seeds(run)
    jobs = [lambda: searcher.boards(seeds)]
    jobs += [(lambda source=source: [searcher.role_source(source)]) for source in SOURCES]
    jobs += [(lambda source=source: [searcher.local_feed(source)]) for source in local_feeds()]
    dossiers = _pending(profile_slug)
    sources: dict[str, dict] = {"watch": {"status": "ok", "results": len(dossiers)}}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for results in pool.map(lambda job: job(), jobs):
            for name, status, found in results:
                sources[name] = status
                dossiers.extend(found)
    ranked = _rank(dossiers, terms)
    cache_postings(profile_slug, [_cache_row(d, {seed_key(s) for s in seeds}) for d in ranked if d.get("url")])
    flags = memory_flags(profile_slug, [d["id"] for d in ranked])
    for dossier in ranked:
        dossier["memory"] = flags[dossier["id"]]
    if new_only:
        ranked = [d for d in ranked if d["memory"] == "new"]
    returned = ranked[:max(1, limit)]
    _mark_served(profile_slug, returned)
    return {"profile": profile_slug, "queries": terms, "sources": sources, "results": len(ranked),
            "dossiers": returned}
