"""``ultron watch``: re-query a profile's watchlist with no agent and report only postings not seen before.

The watch queries the watchlist boards, keeps postings that match the profile's target titles or role family
and are not clearly ineligible, and drops those the profile already saw, judged or has pending. The rest are
recorded as pending, written to a digest, and handed to the next scout run, which serves them first. The
watch gives no verdict and uses no tokens.
"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from ultron.core import presets
from ultron.core.ats.clients import AtsClientError, fetch_vendor_jobs
from ultron.core.discovery.prefilter import is_clearly_ineligible_at_discovery, passes_coarse_prefilter
from ultron.core.discovery.title_vocabulary import build_title_vocabulary
from ultron.core.remote_requirement import is_remote_required
from ultron.core.reporting.export import escape_markdown_cell
from ultron.core.rules.geography import geo_scope, location_scope
from ultron.core.vocabulary import active_pack
from ultron.infra import home
from ultron.infra.net import HostBlocked, HttpError, RequestBudgetExceeded, URLRejected
from ultron.profiles import store as profile_store
from ultron.stages.candidates import candidate_identity
from ultron.stages.discover import clean_posting, posting_candidate, seed_key
from ultron.stages.http_factory import HOST_BLOCK_THRESHOLD, client_for_run
from ultron.stages.scout import WATCH_PASS, watch_seeds
from ultron.storage import ledger, scout_memory, watchlist

WATCH_DIR = "watch"


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


def _write_digest(profile_slug: str, found: list[dict], stamp: str) -> str:
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
    path = folder / f"{profile_slug}-{stamp}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    (folder / f"{profile_slug}-{stamp}.json").write_text(json.dumps(found, ensure_ascii=False, indent=2),
                                                         encoding="utf-8")
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
    if not seeds:
        return {"profile": profile_slug, "boards": 0, "new_postings": 0, "digest": None,
                "hint": "the watchlist is empty: run a scout search first, or add boards with `ultron watchlist add`"}
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
    return {
        "profile": profile_slug,
        "boards": len(seeds),
        "boards_failed": errors,
        "matched": len(matched),
        "new_postings": len(found),
        "digest": _write_digest(profile_slug, found, stamp) if found else None,
        "requests": client.requests_made,
    }
