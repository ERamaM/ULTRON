"""``ultron watch``: day 2 and later with no agent: what is new for a profile, and what closed.

The watch queries the watchlist boards and the keyless role-searchable job APIs, keeps postings that match the
profile's target titles or role family and are not clearly ineligible, and drops those the profile already saw,
judged or has pending. The rest are recorded as pending and served first by the next ``ultron search`` (or scout
run). It also re-reads the postings of the profile's latest shortlist and reports the ones that closed. Both go
to a digest. The watch gives no verdict and uses no tokens.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from ultron.core import presets
from ultron.core.ats.clients import AtsClientError, fetch_vendor_jobs
from ultron.core.ats.clients.base import AtsPosting
from ultron.core.decision.unit import load_allowed_domains
from ultron.core.discovery.prefilter import is_clearly_ineligible_at_discovery, passes_coarse_prefilter
from ultron.core.discovery.title_vocabulary import build_title_vocabulary
from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.feeds.urls import is_aggregator_host
from ultron.core.remote_requirement import is_remote_required
from ultron.core.reporting.export import escape_markdown_cell
from ultron.core.rules.geography import geo_scope, location_scope
from ultron.core.vocabulary import active_pack
from ultron.infra import home
from ultron.infra.fs import atomic_write_text
from ultron.infra.net import HostBlocked, HttpError, RequestBudgetExceeded, URLRejected, top_hosts
from ultron.profiles import store as profile_store
from ultron.stages.candidates import candidate_identity
from ultron.stages.discover import clean_posting, posting_candidate, seed_key
from ultron.stages.fetch import fetch_posting, posting_status
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, client_for_run
from ultron.stages.role_feeds import (
    keyless_sources,
    known_resolutions,
    local_feed_rows,
    local_feeds,
    query_terms,
    remember_resolutions,
    role_source_rows,
)
from ultron.stages.scout_finds import web_candidate
from ultron.stages.scout_resolve import resolve_aggregator
from ultron.stages.watch_boards import WATCH_PASS, watch_seeds
from ultron.storage import ledger, runs, scout_memory, watchlist

WATCH_DIR = "watch"
RECHECK_MAX = 25
MAX_RESOLVED = 20  # aggregator pages followed to the employer posting per watch


def profile_matches(postings: list, profile: dict) -> list:
    """Board postings that match the profile's titles or role family and are not clearly ineligible."""
    criteria = profile.get("criteria") or {}
    pack, geo, scope = active_pack(profile), geo_scope(profile), location_scope(profile)
    vocabulary = build_title_vocabulary(criteria, vocabulary=pack)
    remote = is_remote_required(criteria)
    return [p for p in postings
            if passes_coarse_prefilter(p, title_vocabulary=vocabulary, vocabulary=pack)
            and not is_clearly_ineligible_at_discovery(p, geo=geo, remote_required=remote,
                                                       excluded_location_terms=scope["excluded_terms"])]


def _query(seed, client, timeout: int) -> tuple[list, str | None]:
    try:
        return [clean_posting(p) for p in fetch_vendor_jobs(seed.ats, seed.board_identifier, client=client,
                                                             timeout=timeout)], None
    except RequestBudgetExceeded as exc:
        return [], f"budget_exhausted: {exc.scope}"
    except (AtsClientError, HostBlocked, HttpError, URLRejected) as exc:
        return [], type(exc).__name__
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return [], f"unexpected_payload: {type(exc).__name__}"


def as_postings(rows: list[dict]) -> list[AtsPosting]:
    """Feed rows (``title``, ``url``, ``location``, ``remote``, ``posted``) as board-like postings for matching.

    Without a ``remote`` hint, the location stands in for it (feeds state remote scope as the location).
    """
    return [AtsPosting(title=r["title"], apply_url=r["url"], location=r.get("location", ""),
                       remote_hint=r.get("remote") or r.get("location", ""), posted_at=r.get("posted") or None)
            for r in rows]


def _role_candidates(run: dict, client, timeout: int) -> tuple[list[dict], dict[str, dict]]:
    """Matching postings of the keyless role APIs and feeds, as candidate rows, plus each source's status."""
    profile = run["profile_snapshot"]
    terms = query_terms(profile)
    statuses: dict[str, dict] = {}
    rows: list[dict] = []
    for source in keyless_sources():
        statuses[source.name], got = role_source_rows(source, terms, client, timeout)
        rows.extend(got)
    for feed in local_feeds():
        statuses[feed["id"]], got = local_feed_rows(feed, client, timeout)
        rows.extend(got)
    company = {r["url"]: r.get("company_name", "") for r in rows}
    source = {r["url"]: r.get("source", "") for r in rows}
    candidates, resolved, known, learned = [], 0, known_resolutions(), {}
    for posting in profile_matches(as_postings(rows), profile):
        url = posting.apply_url
        if is_aggregator_host(domain_of(normalize_url(url))):
            # A scout run drops aggregator pages, so only the employer posting they link to is recorded. Links
            # followed before are reused, so the per-watch bound only applies to new listings and never starves them.
            if url not in known:
                if resolved >= MAX_RESOLVED:
                    continue
                resolved += 1
                found = resolve_aggregator(url, client, timeout, company[url])
                known[url] = learned[url] = found.url if found.outcome == "resolved_aggregator" else ""
            if not known[url]:
                continue
            url = known[url]
        candidate = web_candidate(url, {"title": posting.title, "company": company[posting.apply_url],
                                        "location": posting.location}, f"watch:{source[posting.apply_url]}")
        candidate.search_pass = WATCH_PASS
        candidates.append(candidate.model_dump(mode="json"))
    remember_resolutions(learned)
    return candidates, statuses


def latest_shortlist(profile_slug: str) -> list[dict]:
    """The rows of the profile's latest finalized run (``results.json``), or ``[]``."""
    for run in runs.list_runs():
        if run.get("profile_slug") == profile_slug and "finalize" in (run.get("stages_done") or []):
            path = runs.run_dir(run["run_id"]) / "results.json"
            try:
                return list(json.loads(path.read_text(encoding="utf-8")).get("rows") or [])
            except (OSError, ValueError):
                return []
    return []


def recheck_closed(profile_slug: str, client) -> list[dict]:
    """Postings of the latest shortlist that no longer read as open (at most ``RECHECK_MAX`` re-read)."""
    allowed = load_allowed_domains()
    closed = []
    for row in latest_shortlist(profile_slug)[:RECHECK_MAX]:
        url = str(row.get("apply_url") or "")
        if not url:
            continue
        try:
            status = posting_status(fetch_posting(web_candidate(url, {}, "recheck"), client, allowed))
        except (HostBlocked, RequestBudgetExceeded):
            continue
        if status == "closed":
            closed.append({"title": row.get("title", ""), "company": row.get("company", ""), "apply_url": url})
    return closed


def _write_digest(profile_slug: str, found: list[dict], stamp: str, closed: list[dict] | None = None) -> str:
    folder = home.ultron_home() / WATCH_DIR
    folder.mkdir(parents=True, exist_ok=True)
    by_company: dict[str, list[dict]] = {}
    for item in found:
        by_company.setdefault(item["ats_company"] or "unknown", []).append(item)
    lines = [f"# Watch digest: {profile_slug}", "", f"{len(found)} new postings, {stamp}", ""]
    for company, items in sorted(by_company.items()):
        lines.append(f"## {escape_markdown_cell(company)}")
        lines += [f"- {escape_markdown_cell(i['ats_title'])} ({escape_markdown_cell(i['ats_location'] or 'location not stated')}):"
                  f" {escape_markdown_cell(i['normalized_url'])}" for i in items]
        lines.append("")
    if closed:
        lines += ["## Closed since the last shortlist", ""]
        lines += [f"- {escape_markdown_cell(c['title'])} ({escape_markdown_cell(c['company'])}):"
                  f" {escape_markdown_cell(c['apply_url'])}" for c in closed]
        lines.append("")
    path = folder / f"{profile_slug}-{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (folder / f"{profile_slug}-{stamp}.json").write_text(json.dumps(found, ensure_ascii=False, indent=2),
                                                         encoding="utf-8")
    atomic_write_text(folder / f"{profile_slug}-{stamp}.closed.json", json.dumps(closed or [], ensure_ascii=False))
    return str(path)


def watch(profile_slug: str, preset: str = presets.DEFAULT_PRESET, overrides: dict | None = None) -> dict[str, Any]:
    """Query the watchlist of ``profile_slug`` and record the postings it has not seen yet.

    ``overrides`` (a saved search's settings) are validated and merged into the preset, so its request limits
    and timeouts apply to the watch too.
    """
    profile = profile_store.load_profile(profile_slug)
    settings = presets.resolve(preset)
    if overrides:
        settings.update(presets.validate_overrides(overrides, settings))
    run = {"profile_slug": profile_slug, "profile_snapshot": profile, "settings": settings}
    seeds = watch_seeds(run)
    client = client_for_run(run, block_threshold=HOST_BLOCK_THRESHOLD)
    timeout = run["settings"]["ats_api_timeout"]
    errors, matched = [], []
    for seed in seeds:
        postings, error = _query(seed, client, timeout)
        if error:
            errors.append({"seed_key": seed_key(seed), "error": error})
            continue
        for posting in profile_matches(postings, profile):
            if (candidate := posting_candidate(seed, posting, search_pass=WATCH_PASS)) is not None:
                matched.append(candidate.model_dump(mode="json"))
    role_matched, sources = _role_candidates(run, client, timeout)
    matched.extend(role_matched)
    closed = recheck_closed(profile_slug, client)
    with closing(ledger.connect()) as conn:
        flags = scout_memory.flags(conn, profile_slug, [candidate_identity(c) for c in matched])
        pending = watchlist.known_pending(conn, profile_slug)
        fresh = {}
        for item in matched:
            key = candidate_identity(item)
            if flags[key] == "new" and key not in pending:
                fresh.setdefault(key, {**item, "identity_key": key})
        added = watchlist.add_pending(conn, profile_slug, fresh.items())
    found = [fresh[key] for key in added]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    result = {
        "profile": profile_slug,
        "boards": len(seeds),
        "boards_failed": errors,
        "sources": sources,
        "matched": len(matched),
        "new_postings": len(found),
        "closed": closed,
        "digest": _write_digest(profile_slug, found, stamp, closed) if found or closed else None,
        "requests": client.requests_made,
        "requests_by_domain": top_hosts(client.requests_by_host()),
    }
    if not seeds:
        result["hint"] = "the watchlist is empty: save a shortlist first, or add boards with `ultron watchlist add`"
    return result
