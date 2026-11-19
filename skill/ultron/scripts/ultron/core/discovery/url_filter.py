from __future__ import annotations

import re
from collections import defaultdict, deque
from collections.abc import Callable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from ultron.core.ats import extract_seed_from_url, infer_vendor_from_host
from ultron.core.ats.vendors import greenhouse_job_url
from ultron.core.models import CandidateUrl, SearchHit
from ultron.core.urlpolicy import URLRejected, admit_url, safe_url_label

# Query parameter keys that identify specific job postings in ATS platforms.
# These must be preserved during URL normalization to avoid false deduplication.
_ATS_SIGNIFICANT_PARAMS = frozenset(
    {
        "gh_jid",
        "jobid",
        "job_id",
        "id",
        "requisitionid",
        "jid",
        "wdi",
        "mode",
        "iis",
        "pid",
        "positionid",
        # Taleo-family job identifiers: classic Taleo uses .../jobdetail.ftl?job=12345;
        # Taleo Business Edition uses .../requisition.jsp?...&rid=123.
        "job",
        "rid",
    }
)


def normalize_url(raw_url: str) -> str:
    raw = raw_url.strip()
    explicit_scheme = re.match(r"^[a-z][a-z0-9+.-]*:", raw, flags=re.IGNORECASE)
    if raw and not explicit_scheme and not raw.startswith("//"):
        raw = f"https://{raw.lstrip('/')}"
    parsed = urlparse(raw)
    scheme = parsed.scheme or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    # Preserve ATS-significant query params, strip tracking/UTM params.
    qs = parse_qs(parsed.query, keep_blank_values=False)
    significant = {k: v for k, v in qs.items() if k.lower() in _ATS_SIGNIFICANT_PARAMS}
    query = urlencode(sorted(significant.items()), doseq=True) if significant else ""
    clean = parsed._replace(scheme=scheme, netloc=netloc, path=path, params="", query=query, fragment="")
    return urlunparse(clean)


def domain_of(url: str) -> str:
    return urlparse(url).netloc.lower()


def _is_domain_allowed(domain: str, allowed_domains: list[str]) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in allowed_domains)


def _is_domain_blocked(domain: str, blocked_domains: set[str]) -> bool:
    return any(domain == d or domain.endswith(f".{d}") for d in blocked_domains)


def _extract_ats_board(query: str) -> tuple[str, str] | None:
    # Query label format: "Company [greenhouse:board]"
    match = re.search(r"\[([a-z0-9_]+):([a-z0-9_./:-]+)\]", query.lower())
    if not match:
        return None
    return match.group(1), match.group(2)


def _canonicalize_greenhouse_from_custom(url: str, query: str) -> str | None:
    ats_board = _extract_ats_board(query)
    if not ats_board:
        return None
    ats_name, board = ats_board
    if ats_name != "greenhouse" or not board:
        return None

    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    def _id_from_params() -> str:
        for key in ("gh_jid", "jobid"):
            candidate = (params.get(key) or [""])[0].strip()
            if candidate.isdigit():
                return candidate
        return ""

    gh_jid = _id_from_params()
    if not gh_jid:
        match = re.search(r"(?:[?&](?:gh_jid|jobid)=)(\d+)", url)
        if match:
            gh_jid = match.group(1)

    if gh_jid.isdigit():
        return greenhouse_job_url(board, gh_jid)
    return None


def canonicalize_url_for_allowlist(url: str, query: str, allowed_domains: list[str]) -> str:
    normalized = normalize_url(url)
    domain = domain_of(normalized)
    if _is_domain_allowed(domain, allowed_domains):
        return normalized
    canonicalized = _canonicalize_greenhouse_from_custom(url, query)
    if canonicalized:
        return normalize_url(canonicalized)
    return normalized


def rebalance_candidates(
    candidates: list[CandidateUrl], max_per_query: int, *, capped: Callable[[CandidateUrl], bool] = lambda _: True
) -> tuple[list[CandidateUrl], list[CandidateUrl]]:
    if max_per_query <= 0:
        return [candidate for candidate in candidates if not capped(candidate)], [
            candidate for candidate in candidates if capped(candidate)
        ]
    if len(candidates) <= 1:
        return list(candidates), []

    queues: dict[str, deque[CandidateUrl]] = {}
    query_order: list[str] = []
    for candidate in candidates:
        if candidate.query_id not in queues:
            queues[candidate.query_id] = deque()
            query_order.append(candidate.query_id)
        queues[candidate.query_id].append(candidate)

    kept: list[CandidateUrl] = []
    overflow: list[CandidateUrl] = []
    taken: dict[str, int] = defaultdict(int)
    active = list(query_order)

    while active:
        next_active: list[str] = []
        for query_id in active:
            queue = queues[query_id]
            if not queue:
                continue
            candidate = queue.popleft()
            if capped(candidate) and taken[query_id] >= max_per_query:
                overflow.append(candidate)
            else:
                kept.append(candidate)
                if capped(candidate):
                    taken[query_id] += 1
            if queue:
                next_active.append(query_id)
        active = next_active

    return kept, overflow


def filter_candidates(
    hits: list[SearchHit],
    rules: dict,
    *,
    is_allowed_domain_fn: Callable[[str, list[str]], bool] = _is_domain_allowed,
) -> tuple[list[CandidateUrl], list[dict]]:
    allowed = rules.get("allowed_ats_domains", [])
    blocked = set(rules.get("blocked_final_domains", []))
    discards: list[dict] = []
    kept: list[CandidateUrl] = []

    seen: set[str] = set()
    for hit in hits:
        if not hit.url:
            continue
        normalized = canonicalize_url_for_allowlist(hit.url, hit.query, allowed)
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            discards.append({"url": safe_url_label(hit.url), "reason": "invalid_url", "detail": "invalid_url"})
            continue
        domain = domain_of(normalized)

        if "linkedin.com" in domain:
            discards.append({"url": hit.url, "reason": "linkedin_forbidden", "detail": domain})
            continue

        if _is_domain_blocked(domain, blocked):
            discards.append({"url": hit.url, "reason": "blocked_domain", "detail": domain})
            continue

        try:
            if allowed and not is_allowed_domain_fn(domain, allowed):
                raise URLRejected("domain_not_allowed")
            # Domain admission uses the pipeline's established injectable
            # policy seam; URL structure and resolved-address admission remain
            # mandatory and cannot be bypassed by that seam.
            admitted = admit_url(normalized)
        except URLRejected as exc:
            discards.append(
                {
                    "url": safe_url_label(normalized),
                    "reason": "url_not_admitted",
                    "detail": exc.reason,
                }
            )
            continue
        normalized = admitted.url
        domain = admitted.hostname

        matched_vendor = str(hit.matched_vendor or infer_vendor_from_host(domain) or "")
        if matched_vendor and extract_seed_from_url(normalized, str(hit.ats_company or hit.company_hint or "")) is None:
            discards.append(
                {
                    "url": safe_url_label(normalized),
                    "reason": "ats_non_board_url",
                    "detail": matched_vendor,
                }
            )
            continue

        if normalized in seen:
            discards.append({"url": hit.url, "reason": "duplicate_url", "detail": normalized})
            continue
        seen.add(normalized)
        kept.append(
            CandidateUrl(
                query_id=hit.query_id,
                query=hit.query,
                source_url=hit.url,
                normalized_url=normalized,
                domain=domain,
                ats_title=hit.ats_title,
                ats_location=hit.ats_location,
                ats_remote_hint=hit.ats_remote_hint,
                ats_company=hit.ats_company,
                source_type=hit.source_type,
                source_key=hit.source_key,
                source_label=hit.source_label,
                query_bucket=hit.query_bucket,
                title_family=hit.title_family,
                site_family=hit.site_family,
                company_hint=hit.company_hint,
                search_pass=hit.search_pass,
                hit_title=hit.title,
                snippet=hit.snippet,
                matched_vendor=matched_vendor,
                query_yield_bucket=hit.query_yield_bucket,
            )
        )

    return kept, discards
