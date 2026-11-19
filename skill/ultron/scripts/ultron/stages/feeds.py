"""``discover feeds``: structured job feeds as candidate, seed and company sources."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict
from types import SimpleNamespace
from urllib.parse import urlparse

import yaml

from ultron import home, runs
from ultron.core.ats.clients import AtsClientError
from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import extract_seed_from_url, is_direct_supported_ats_host
from ultron.core.ats.vendors import SUPPORTED_ATS_DOMAINS, domain_matches
from ultron.core.discovery.prefilter import (
    is_clearly_ineligible_at_discovery,
    passes_coarse_prefilter,
    posting_rank_score,
)
from ultron.core.discovery.title_vocabulary import build_title_vocabulary
from ultron.core.discovery.url_filter import domain_of, normalize_url
from ultron.core.feeds.collectors import COLLECTORS
from ultron.core.feeds.urls import AGGREGATOR_DOMAINS, SHORTENER_DOMAINS, candidate_urls_from_row, redirect_targets
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import AtsBoardSeed, CandidateUrl
from ultron.core.remote_requirement import is_remote_required
from ultron.core.rules.geography import geo_scope, location_scope, scoped_eligibility_terms
from ultron.core.text_clean import clean_text
from ultron.core.vocabulary import active_pack
from ultron.http_factory import client_for_run
from ultron.net import HttpError, RequestBudgetExceeded, URLRejected
from ultron.seeds import FEED_PROPOSALS_FILE, LEGAL_COMPANY_SUFFIXES, Proposal, SeedFileError, add_seeds, load_inventory

FEED_PREFIX = "feed:"
PROPOSALS_FILE = FEED_PROPOSALS_FILE
ERRORS_FILE = "feed_errors.jsonl"


def load_sources() -> list[dict]:
    """Enabled sources of the bundled ``feeds.yaml`` that have a known parser."""
    path = home.data_dir() / "feeds.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rows = data.get("sources", []) if isinstance(data, dict) else []
    return [
        s for s in rows
        if isinstance(s, dict) and s.get("enabled", True) and s.get("id") and s.get("parser") in COLLECTORS
    ]


def _identity(row: dict) -> str:
    return canonical_job_identity(
        row.get("source_url") or row.get("normalized_url") or "",
        source_type=row.get("source_type") or "",
        source_key=row.get("source_key") or "",
    ).identity_key


def _posting_like(row: dict) -> SimpleNamespace:
    remote = row.get("remote_hint") or ("remote" if row.get("remote") is True else "")
    return SimpleNamespace(
        title=clean_text(row.get("title")),
        location=clean_text(row.get("location")),
        remote_hint=str(remote or ""),
        apply_url=str(row.get("url") or ""),
    )


_AGGREGATORS = {d.removeprefix("www.") for d in AGGREGATOR_DOMAINS}
# Names too generic to match across TLDs (``remote.co`` must not claim ``remote.de`` or ``remote.acme.de``).
_GENERIC_AGGREGATOR_NAMES = frozenset({"remote"})
_AGGREGATOR_NAMES = {d.split(".")[0] for d in _AGGREGATORS} - _GENERIC_AGGREGATOR_NAMES
_COUNTRY_SLDS = frozenset({"co", "com", "org", "net"})  # arbeitnow.co.uk, foo.com.au
_GENERIC_HOST_LABELS = frozenset({"www", "jobs", "careers", "apply", "boards", "job", "work", "team"})
# Legal forms plus product words that do not distinguish a company's domain label.
_COMPANY_SUFFIXES = LEGAL_COMPANY_SUFFIXES | frozenset({"labs", "lab", "ai", "hq", "io", "app"})
_THIRD_PARTY_DOMAINS = frozenset({
    "github.com",
    "gitlab.com",
    "linkedin.com",
    "ycombinator.com",
    "notion.so",
    "notion.site",
    "medium.com",
    "twitter.com",
    "x.com",
    "facebook.com",
    "google.com",
    "docs.google.com",
    "angel.co",
    "wellfound.com",
    "lever.co",
    "greenhouse.io",
})


def _is_disallowed_proposal_host(host: str) -> bool:
    if is_aggregator_host(host) or is_direct_supported_ats_host(host):
        return True
    return any(
        domain_matches(host, d)
        for d in (*_THIRD_PARTY_DOMAINS, *SUPPORTED_ATS_DOMAINS, *SHORTENER_DOMAINS)
    )


def _normalize_company_token(text: str) -> str:
    """Lowercase ASCII-folded alphanumerics with trailing legal/product suffixes stripped."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    # Join dotted abbreviations like "S.L." / "S.A." before tokenizing so they stay one suffix token.
    folded = re.sub(r"\b([a-z])\.([a-z])\.?", r"\1\2", folded)
    tokens = re.findall(r"[a-z0-9]+", folded)
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return "".join(tokens)


def _label_matches_company(label: str, company: str) -> bool:
    """The registrable domain's main label names ``company`` (not just contains its letters).

    ``Box`` must not claim ``dropbox.com`` or ``Meta`` ``metabase.com``: a short company name has to
    cover most of the label; a longer one may sit at its start or end (``spotify`` in
    ``lifeatspotify``); and a label that is a prefix or suffix of the full company name matches
    (``telekom`` for ``Deutsche Telekom``).
    """
    norm_label = _normalize_company_token(label)
    stripped = _normalize_company_token(company)
    full = re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKD", company).encode("ascii", "ignore").decode("ascii").lower())
    if len(stripped) < 3 or len(norm_label) < 3:
        return False
    if norm_label == stripped or norm_label == full:
        return True
    if (norm_label.startswith(stripped) or norm_label.endswith(stripped)) and (
        len(stripped) >= 5 or len(stripped) / len(norm_label) > 0.5
    ):
        return True
    return len(norm_label) >= 5 and any(f.startswith(norm_label) or f.endswith(norm_label) for f in (stripped, full))


def _company_domain_from_url(url: str, company: str = "") -> str:
    """Return a company's own domain from ``url``, or ``""`` for generic, third-party, or mismatched hosts."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host or "." not in host or ":" in host:
        return ""
    if _is_disallowed_proposal_host(host):
        return ""
    labels = [part for part in host.split(".") if part]
    if len(labels) < 2 or all(part.isdigit() for part in labels):
        return ""
    while len(labels) > 2 and labels[0] in _GENERIC_HOST_LABELS:
        labels.pop(0)
    if labels[0] in _GENERIC_HOST_LABELS:
        return ""
    if len(labels) == 2 and labels[0] in _COUNTRY_SLDS and len(labels[1]) == 2:
        return ""
    candidate = ".".join(labels)
    if _is_disallowed_proposal_host(candidate):
        return ""
    # Compare only the registrable domain's main label against the company name so generic or
    # third-party subdomains (e.g. hiring.acme.com for Foo, foo.airtable.com) never slip through.
    if len(labels) >= 3 and labels[-2] in _COUNTRY_SLDS and len(labels[-1]) == 2:
        reg_labels = labels[-3:]
    else:
        reg_labels = labels[-2:]
    registrable = ".".join(reg_labels)
    if _is_disallowed_proposal_host(registrable):
        return ""
    if not _label_matches_company(reg_labels[0], company):
        return ""
    return registrable


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
    return len(labels) == 1 or (len(labels) == 2 and labels[0] in _COUNTRY_SLDS)


def _direct_ats_url(row: dict) -> str:
    """First URL of the row on a supported ATS host (MARK III ``harvest_ats_urls_from_rows``), else ``""``."""
    for url in candidate_urls_from_row(row):
        host = urlparse(url).netloc.lower()
        if host and is_direct_supported_ats_host(host):
            return url
    return ""


def _candidate(source: dict, row: dict, posting: SimpleNamespace) -> CandidateUrl | None:
    """Candidate for a feed posting: its direct ATS URL when the row has one, else its own URL.

    A row with no direct ATS URL whose URL is an aggregator's page gives no candidate (MARK III only
    harvests direct ATS URLs from feeds; such pages are outside the allowlist and never kept).
    """
    url = _direct_ats_url(row) or posting.apply_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or is_aggregator_host(parsed.netloc):
        return None
    normalized = normalize_url(url)
    key = f"{FEED_PREFIX}{source['id']}"
    company = clean_text(row.get("company_name"))
    snippet = " | ".join(part for part in (posting.location, posting.remote_hint) if part).strip()
    return CandidateUrl(
        query_id=key,
        query="",
        source_url=url,
        normalized_url=normalized,
        domain=domain_of(normalized),
        ats_title=posting.title,
        ats_location=posting.location,
        ats_remote_hint=posting.remote_hint,
        ats_company=company,
        source_type=key,
        source_key=key,
        source_label=f"Feed {source['id']}",
        hit_title=posting.title,
        snippet=snippet,
        company_hint=company,
    )


def _error(source: dict, error: str, **extra) -> dict:
    return {"source": source["id"], "error": error, **extra}


def _collect(client, source: dict, timeout: int, errors: list[dict]) -> tuple[list[dict], str]:
    """Return ``(rows, status)`` with status ok | failed | skipped | budget_spent; never raises per-source errors."""
    try:
        rows, discards = COLLECTORS[source["parser"]](client, source, timeout)
    except RequestBudgetExceeded as exc:
        if exc.scope == "host":
            errors.append(_error(source, "host_budget_exhausted", host=exc.host, skipped=True))
            return [], "skipped"
        errors.append(_error(source, "request_budget_exhausted", skipped=True))
        return [], "budget_spent"
    except AtsClientError as exc:
        errors.append(_error(source, str(exc)))
        return [], "failed"
    except HttpError as exc:
        errors.append(_error(source, f"http_error: {exc} (status={exc.status_code})"))
        return [], "failed"
    except URLRejected as exc:
        errors.append(_error(source, f"url_rejected: {exc}"))
        return [], "failed"
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(_error(source, f"unexpected_payload: {exc!r}"))
        return [], "failed"
    failed = bool(discards) and not rows
    for discard in discards:
        detail = str(discard.get("detail") or discard.get("reason"))
        errors.append(_error(source, detail, url=discard.get("url"), partial=not failed))
    return rows, "failed" if failed else "ok"


def discover_feeds(run_id: str) -> dict:
    run = runs.load_run(run_id)
    settings = run["settings"]
    out_dir = runs.run_dir(run_id)
    profile = run.get("profile_snapshot") or {}
    profile_slug = run["profile_slug"]
    criteria = profile.get("criteria") or {}
    pack = active_pack(profile)
    geo = geo_scope(profile)
    scope = location_scope(profile)
    elig_terms = scoped_eligibility_terms(profile)
    remote_required = is_remote_required(criteria)
    title_vocabulary = build_title_vocabulary(criteria, vocabulary=pack)
    timeout = settings["ats_api_timeout"]
    # Large JSON feeds (e.g. arbeitnow ~10 MB) exceed the default 5 MB page cap.
    client = client_for_run(run, max_bytes=15_000_000)

    inventory = load_inventory(profile_slug)
    known_boards = {seed_identity(s.ats, s.board_identifier) for s in inventory}
    known_companies = {s.company.strip().lower() for s in inventory if s.company}

    errors: list[dict] = []
    found: list[tuple[int, CandidateUrl]] = []
    new_seeds: dict[tuple[str, str], AtsBoardSeed] = {}
    proposals: dict[str, Proposal] = {}
    counts = {"ok": 0, "failed": 0, "skipped": 0}
    budget_spent = False

    for source in load_sources():
        if budget_spent:
            errors.append(_error(source, "request_budget_exhausted", skipped=True))
            counts["skipped"] += 1
            continue
        rows, status = _collect(client, source, timeout, errors)
        if status == "budget_spent":
            budget_spent = True
            counts["skipped"] += 1
            continue
        counts[status] += 1
        company_list = bool(source.get("company_list"))
        for row in rows:
            if not isinstance(row, dict):
                continue
            company = clean_text(row.get("company_name"))
            posting = _posting_like(row)
            relevant = passes_coarse_prefilter(posting, title_vocabulary=title_vocabulary, vocabulary=pack)
            ats_hit = False
            for url in candidate_urls_from_row(row):
                for link in (url, *redirect_targets(url)):
                    seed = extract_seed_from_url(link, company)
                    if seed is None:
                        continue
                    ats_hit = True
                    key = seed_identity(seed.ats, seed.board_identifier)
                    if key not in known_boards and key not in new_seeds:
                        new_seeds[key] = seed.model_copy(update={
                            "source_type": "feed_discovered",
                            "source_label": f"Feed {source['id']}",
                            "source_url": link,
                        })
            if (
                not company_list
                and relevant
                and not is_clearly_ineligible_at_discovery(
                    posting, geo=geo, remote_required=remote_required, excluded_location_terms=scope["excluded_terms"],
                )
            ):
                candidate = _candidate(source, row, posting)
                if candidate is not None:
                    score = posting_rank_score(
                        posting,
                        title_vocabulary=title_vocabulary,
                        vocabulary=pack,
                        geo=geo,
                        remote_required=remote_required,
                        preferred_regions=scope["preferred"],
                        eligibility_terms=elig_terms,
                        excluded_location_terms=scope["excluded_terms"],
                    )
                    found.append((score, candidate))
            if company and not ats_hit and (company_list or relevant):
                low = company.lower()
                if low not in proposals and low not in known_companies:
                    url = str(row.get("url") or "").strip()
                    domain = _company_domain_from_url(url, company)
                    source_url = url if urlparse(url).scheme in {"http", "https"} else ""
                    proposals[low] = Proposal(company=company, domain=domain, source_url=source_url)

    seeds_added = 0
    if new_seeds:
        try:
            seeds_added = add_seeds(list(new_seeds.values()), profile_slug=profile_slug)
        except SeedFileError as exc:
            errors.append({"source": "seeds", "error": f"seed_file_corrupt: {exc}"})

    # Rewrite only this stage's rows (other stages' rows stay); the first row per identity wins.
    existing = runs.read_jsonl(out_dir / "candidates.jsonl")
    kept_rows = [r for r in existing if not str(r.get("source_type") or "").startswith(FEED_PREFIX)]
    seen = {_identity(r) for r in kept_rows}
    candidates: list[CandidateUrl] = []
    for _score, candidate in sorted(found, key=lambda item: item[0], reverse=True):
        identity = _identity(candidate.model_dump())
        if identity in seen:
            continue
        seen.add(identity)
        candidates.append(candidate)

    runs.write_jsonl(out_dir / "candidates.jsonl", [*kept_rows, *candidates])
    runs.write_jsonl(out_dir / PROPOSALS_FILE, (asdict(p) for p in proposals.values()))
    runs.write_jsonl(out_dir / ERRORS_FILE, errors)
    runs.mark_stage(run_id, "discover_feeds")
    return {
        "sources_ok": counts["ok"],
        "sources_failed": counts["failed"],
        "sources_skipped": counts["skipped"],
        "candidates": len(candidates),
        "seeds_added": seeds_added,
        "company_hints": len(proposals),
        "requests": client.requests_made,
    }
