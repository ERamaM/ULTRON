from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

from ultron.core.ats.clients.candidatemanager import candidatemanager_board_from_url
from ultron.core.ats.clients.occupop import API_HOST as OCCUPOP_API_HOST
from ultron.core.ats.vendors import (
    VENDOR_DATA_BY_NAME,
    AtsVendorData,
    AtsVendorName,
    canonical_domain_for_vendor,
    domain_matches,
    match_vendor_by_domain,
    match_vendor_by_fingerprint,
)
from ultron.core.extractors.base import parse_generic
from ultron.core.extractors.parser_factory import ParserFn, parser_for_vendor
from ultron.core.models import AtsBoardSeed, ExtractedJob, JobRawPage

SeedExtractorFn = Callable[[str, str], str | None]
# Board identifiers kept in their original case: Workday site segments, Occupop share tokens, CandidateManager ids.
CASE_SENSITIVE_BOARD_VENDORS = frozenset({"workday", "occupop", "candidatemanager"})
_OPAQUE_BOARD_VENDORS = frozenset({"occupop", "candidatemanager"})

GENERIC_LABELS = {
    "api",
    "www",
    "jobs",
    "job",
    "careers",
    "career",
    "developer",
    "developers",
    "docs",
    "apply",
    "app",
    "global",
    "international",
    "status",
    "support",
    "eu",
    "emea",
    "us",
    "uk",
    "en",
    "en-us",
    "de",
    "fr",
    "es",
}
GENERIC_BOARD_IDENTIFIERS = {
    "jobs",
    "job",
    "career",
    "careers",
    "apply",
    "app",
    "team",
    "company",
    "companies",
    "global",
    "international",
    "eu",
    "emea",
    "uk",
    "us",
    "default",
    # Marketing-site path segments that must never be minted as board tokens
    # (e.g. greenhouse.io/customers, www.smartrecruiters.com/blog).
    "customers",
    "customer-stories",
    "blog",
    "pricing",
    "about",
    "about-us",
    "product",
    "products",
    "solutions",
    "resources",
    "press",
    "news",
    "events",
    "partners",
    "legal",
    "privacy",
    "terms",
    "contact",
    "demo",
    "platform",
    "integrations",
    "developers",
    "docs",
    "login",
    "signup",
}
GREENHOUSE_RESERVED = {"integrations", "api", "embed", "jobs", "job", "blog", "developers", "docs", "about"}
# Hosts that actually serve Greenhouse job boards. The bare apex / www hosts
# (greenhouse.io, www.greenhouse.io) are marketing pages, not boards.
GREENHOUSE_BOARD_HOSTS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "boards.eu.greenhouse.io",
    "job-boards.eu.greenhouse.io",
)
BOARD_IDENTIFIER_RE = re.compile(r"[^a-zA-Z0-9_.:/-]+")
# Vendors whose hosted boards live on the vendor's www host (www.comeet.com/jobs/<slug>/...,
# www.candidatemanager.net/cm/p/pJobs.aspx?mid=...&sid=...). Their seed extractors accept only board paths.
_WWW_BOARD_VENDORS = frozenset({"comeet", "candidatemanager"})
# Older SmartRecruiters posting links: www.smartrecruiters.com/<Company>/<numeric id>-<title>.
_SMARTRECRUITERS_WWW_POSTING_RE = re.compile(r"/[^/]+/\d+(?:[-/]|$)")


@dataclass(frozen=True, slots=True)
class AtsVendorSpec:
    data: AtsVendorData
    seed_extractor: SeedExtractorFn | None
    parser: ParserFn = field(init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "parser", parser_for_vendor(self.data.name))

    @property
    def name(self) -> AtsVendorName:
        return self.data.name

    @property
    def canonical_domain(self) -> str:
        return self.data.canonical_domain

    @property
    def supports_dynamic_fallback(self) -> bool:
        return self.data.supports_dynamic_fallback

    @property
    def domains(self) -> tuple[str, ...]:
        return self.data.domains


def _normalize_board_identifier(raw: str, *, preserve_case: bool = False) -> str:
    text = str(raw or "").strip()
    if not preserve_case:
        # Most vendors use case-insensitive board tokens; Workday CXS site
        # segments are case-sensitive and must keep their original case.
        text = text.lower()
    clean = BOARD_IDENTIFIER_RE.sub("", text).strip("/")
    if not clean:
        return ""
    if clean.lower() in GENERIC_BOARD_IDENTIFIERS:
        return ""
    return clean


def _host_prefix_labels(host: str, suffix: str) -> list[str] | None:
    """Host labels left of a vendor domain suffix, or None when host doesn't match."""
    if not domain_matches(host, suffix):
        return None
    prefix = host[: len(host) - len(suffix)].strip(".")
    return [label for label in prefix.split(".") if label]


def _is_marketing_vendor_host(host: str, suffix: str) -> bool:
    """True for the bare apex or www host of a vendor domain (marketing site, not a board)."""
    labels = _host_prefix_labels(host, suffix)
    return labels is not None and (not labels or labels == ["www"])


def _non_generic_host_labels(host: str) -> list[str]:
    labels: list[str] = []
    for label in str(host or "").strip().lower().split("."):
        if not label or label in GENERIC_LABELS:
            continue
        labels.append(label)
    return labels


def _slugify_company(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    raw = raw.replace("&", " and ")
    raw = re.sub(r"[^a-z0-9\s\-_.]", " ", raw)
    parts = [part for part in re.split(r"[\s._-]+", raw) if part]
    return "-".join(parts)


def _simple_company(seed_company: str, board_identifier: str) -> str:
    company_hint = str(seed_company or "").strip()
    if not company_hint:
        return board_identifier.replace("-", " ").replace("_", " ").title()
    company_slug = _slugify_company(company_hint)
    board_slug = _slugify_company(board_identifier.split("/", 1)[0])
    if (
        company_slug
        and board_slug
        and (company_slug == board_slug or company_slug in board_slug or board_slug in company_slug)
    ):
        return company_hint
    return board_identifier.split("/", 1)[0].replace("-", " ").replace("_", " ").title()


def _greenhouse_seed(url: str, company_hint: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [part.strip().lower() for part in parsed.path.split("/") if part.strip()]
    params = parse_qs(parsed.query)
    if any(domain_matches(host, item) for item in GREENHOUSE_BOARD_HOSTS) and parts:
        candidate = parts[0]
        if candidate not in GREENHOUSE_RESERVED:
            return _normalize_board_identifier(candidate)
    if domain_matches(host, "greenhouse.io"):
        # Embed URLs (boards.greenhouse.io/embed/job_board?for=acme&token=...)
        # carry the authoritative board token in the "for" query param.
        for key in ("for", "token"):
            values = params.get(key) or []
            candidate = str(values[0]).strip().lower() if values else ""
            if not candidate or candidate.isdigit():
                continue
            normalized = _normalize_board_identifier(candidate)
            if normalized and normalized not in GREENHOUSE_RESERVED:
                return normalized
    jid_values = params.get("gh_jid") or (params.get("jobid") if domain_matches(host, "greenhouse.io") else None) or [""]
    gh_jid = jid_values[0].strip()
    if gh_jid:
        candidates: list[str] = []
        if company_hint:
            candidates.append(_slugify_company(company_hint))
        candidates.extend(_non_generic_host_labels(host))
        for candidate in candidates:
            normalized = _normalize_board_identifier(candidate)
            if normalized and normalized not in GREENHOUSE_RESERVED:
                return normalized
    return None


def _lever_seed(url: str, _company_hint: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in {"hire.lever.co", "hire.eu.lever.co"}:
        return None
    parts = [part.strip().lower() for part in parsed.path.split("/") if part.strip()]
    if host in {"jobs.lever.co", "jobs.eu.lever.co"}:
        return _normalize_board_identifier(parts[0] if parts else "")
    if domain_matches(host, "lever.co"):
        if _is_marketing_vendor_host(host, "lever.co"):
            # Bare apex / www marketing pages (lever.co/customers) are not boards.
            return None
        labels = [label for label in (_host_prefix_labels(host, "lever.co") or []) if label not in GENERIC_LABELS]
        if labels:
            return _normalize_board_identifier(labels[0])
        return _normalize_board_identifier(parts[0] if parts else "")
    return None


def _ashby_seed(url: str, _company_hint: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [part.strip().lower() for part in parsed.path.split("/") if part.strip()]
    if host == "jobs.ashbyhq.com":
        return _normalize_board_identifier(parts[0] if parts else "")
    if domain_matches(host, "ashbyhq.com"):
        if _is_marketing_vendor_host(host, "ashbyhq.com"):
            # Bare apex / www marketing pages (ashbyhq.com/customers) are not boards.
            return None
        labels = [label for label in (_host_prefix_labels(host, "ashbyhq.com") or []) if label not in GENERIC_LABELS]
        if labels:
            return _normalize_board_identifier(labels[0])
        return _normalize_board_identifier(parts[0] if parts else "")
    return None


def _workable_seed(url: str, _company_hint: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [part.strip().lower() for part in parsed.path.split("/") if part.strip()]
    if host == "apply.workable.com" and parts:
        if parts[0] in {"j", "jobs"}:
            return ""
        return _normalize_board_identifier(parts[0])
    return None


def _subdomain_seed(url: str, _company_hint: str, suffix: str) -> str | None:
    host = urlparse(url).netloc.lower()
    prefix_labels = _host_prefix_labels(host, suffix)
    if prefix_labels is None:
        return None
    # Only labels left of the vendor suffix qualify as tenant slugs; the bare
    # apex or www host is the vendor's marketing site, never a board.
    labels = [label for label in prefix_labels if label not in GENERIC_LABELS]
    if not labels:
        return None
    return _normalize_board_identifier(labels[0])


def _path_seed(
    url: str, _company_hint: str, *, host_suffix: str, marker: str | None = None, index: int = 0
) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not domain_matches(host, host_suffix):
        return None
    parts = [part.strip().lower() for part in parsed.path.split("/") if part.strip()]
    if marker and marker in parts:
        marker_idx = parts.index(marker)
        if len(parts) > marker_idx + 1:
            return _normalize_board_identifier(parts[marker_idx + 1])
    if len(parts) > index:
        return _normalize_board_identifier(parts[index])
    return None


def _smartrecruiters_seed(url: str, _company_hint: str) -> str | None:
    candidate = _path_seed(url, "", host_suffix="smartrecruiters.com", marker="company")
    if candidate:
        return candidate
    return _subdomain_seed(url, "", "smartrecruiters.com")


def _jobvite_seed(url: str, company_hint: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    parts = [part.strip().lower() for part in parsed.path.split("/") if part.strip()]
    if host in {"jobs.jobvite.com", "jobs.eu.jobvite.com"}:
        return _normalize_board_identifier(parts[0] if parts else "")
    candidate = _subdomain_seed(url, company_hint, "jobvite.com")
    if candidate:
        return candidate
    if _is_marketing_vendor_host(host, "jobvite.com"):
        # Bare apex / www marketing pages (jobvite.com/customers) are not boards.
        return None
    return _normalize_board_identifier(parts[0] if parts else "")


def _factorial_seed(url: str, company_hint: str) -> str | None:
    candidate = _path_seed(url, company_hint, host_suffix="factorialhr.com", index=0)
    if candidate:
        return candidate
    return _subdomain_seed(url, company_hint, "factorialhr.com")


def _jazzhr_seed(url: str, company_hint: str) -> str | None:
    candidate = _subdomain_seed(url, company_hint, "applytojob.com")
    if candidate:
        return candidate
    company_slug = _slugify_company(company_hint)
    return _normalize_board_identifier(company_slug)


def _pinpoint_seed(url: str, _company_hint: str) -> str | None:
    candidate = _subdomain_seed(url, "", "pinpointhq.com")
    if candidate:
        return candidate
    return _path_seed(url, "", host_suffix="pinpointhq.com", marker="postings")


def _comeet_seed(url: str, company_hint: str) -> str | None:
    parsed = urlparse(url)
    parts = [part.strip().lower() for part in parsed.path.split("/") if part.strip()]
    host = parsed.netloc.lower()
    is_comeet_host = domain_matches(host, "comeet.com") or domain_matches(host, "comeet.co")
    if is_comeet_host:
        for suffix in ("comeet.com", "comeet.co"):
            candidate = _subdomain_seed(url, "", suffix)
            if candidate:
                return candidate
        for marker in ("jobs", "job"):
            if marker not in parts:
                continue
            marker_idx = parts.index(marker)
            neighbors = [
                parts[marker_idx + 1] if len(parts) > marker_idx + 1 else "",
                parts[marker_idx - 1] if marker_idx > 0 else "",
            ]
            for part in neighbors:
                normalized = _normalize_board_identifier(part)
                if normalized and not normalized.isdigit():
                    return normalized
        return None
    return _normalize_board_identifier(_slugify_company(company_hint))


def _workday_seed(url: str, _company_hint: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not (domain_matches(host, "myworkdayjobs.com") or domain_matches(host, "myworkdaysite.com")):
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    prefix_limit = 4 if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]) else 3
    prefix_parts: list[str] = []
    for part in parts:
        low = part.lower()
        # "job" (classic) and "details" (modern .../details/{slug} URLs) both
        # mark the start of the posting path, after the CXS site segment.
        if low in {"job", "details"}:
            break
        prefix_parts.append(part)
        if len(prefix_parts) >= prefix_limit:
            break
    if not prefix_parts:
        prefix_parts = parts[:2]
    # Shared wdN hosts may serve many recruiting tenants. Dropping the path
    # merges unrelated sites and prevents deterministic CXS verification.
    locator = f"{host}/{'/'.join(prefix_parts)}" if prefix_parts else host
    # Workday CXS site segments are case-sensitive (External != external).
    return _normalize_board_identifier(locator, preserve_case=True)


def _icims_seed(url: str, _company_hint: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not domain_matches(host, "icims.com"):
        return None
    return _normalize_board_identifier(host)


def _taleo_seed(url: str, _company_hint: str) -> str | None:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if not domain_matches(host, "taleo.net"):
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    if "careersection" in [part.lower() for part in parts]:
        idx = [part.lower() for part in parts].index("careersection")
        if len(parts) > idx + 1:
            return _normalize_board_identifier(f"{host}/careersection/{parts[idx + 1]}")
    return _normalize_board_identifier(host)


def _occupop_seed(url: str, _company_hint: str) -> str | None:
    """The share token of an employer's embedded job list (``api.occupop.com/api/jobs-frame/<token>``).

    A posting link (``/shared/job/<ref>``) names no board, so it gives no seed.
    """
    parsed = urlparse(url)
    if (parsed.hostname or "").lower() != OCCUPOP_API_HOST:
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) == 3 and parts[:2] == ["api", "jobs-frame"]:
        return parts[2]
    return None


def _candidatemanager_seed(url: str, _company_hint: str) -> str | None:
    return candidatemanager_board_from_url(url)


# Ashby keeps a real parser module (scoped-search fallback); every other vendor's
# page parser is built from the declarative selector table.
VENDOR_SPECS: tuple[AtsVendorSpec, ...] = (
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["greenhouse"],
        _greenhouse_seed,
    ),
    AtsVendorSpec(VENDOR_DATA_BY_NAME["lever"], _lever_seed),
    AtsVendorSpec(VENDOR_DATA_BY_NAME["ashby"], _ashby_seed),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["workable"],
        _workable_seed,
    ),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["personio"],
        lambda url, hint: _subdomain_seed(url, hint, "personio.de") or _subdomain_seed(url, hint, "personio.com"),
    ),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["recruitee"],
        lambda url, hint: _subdomain_seed(url, hint, "recruitee.com"),
    ),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["teamtailor"],
        lambda url, hint: _subdomain_seed(url, hint, "teamtailor.com"),
    ),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["smartrecruiters"],
        _smartrecruiters_seed,
    ),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["homerun"],
        lambda url, hint: _subdomain_seed(url, hint, "homerun.co"),
    ),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["jobvite"],
        _jobvite_seed,
    ),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["bamboohr"],
        lambda url, hint: _subdomain_seed(url, hint, "bamboohr.com"),
    ),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["breezy"],
        lambda url, hint: _subdomain_seed(url, hint, "breezy.hr"),
    ),
    AtsVendorSpec(VENDOR_DATA_BY_NAME["jazzhr"], _jazzhr_seed),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["factorial"],
        _factorial_seed,
    ),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["pinpoint"],
        _pinpoint_seed,
    ),
    AtsVendorSpec(VENDOR_DATA_BY_NAME["comeet"], _comeet_seed),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["workday"],
        _workday_seed,
    ),
    AtsVendorSpec(VENDOR_DATA_BY_NAME["icims"], _icims_seed),
    AtsVendorSpec(VENDOR_DATA_BY_NAME["taleo"], _taleo_seed),
    AtsVendorSpec(
        VENDOR_DATA_BY_NAME["hirehive"],
        lambda url, hint: _subdomain_seed(url, hint, "hirehive.com"),
    ),
    AtsVendorSpec(VENDOR_DATA_BY_NAME["occupop"], _occupop_seed),
    AtsVendorSpec(VENDOR_DATA_BY_NAME["candidatemanager"], _candidatemanager_seed),
)

SPEC_BY_NAME = {spec.name: spec for spec in VENDOR_SPECS}


def iter_vendor_specs() -> Iterable[AtsVendorSpec]:
    return VENDOR_SPECS


def get_vendor_spec(vendor: str | None) -> AtsVendorSpec | None:
    if not vendor:
        return None
    return SPEC_BY_NAME.get(str(vendor).strip().lower())


def match_vendor_spec_by_domain(domain: str) -> AtsVendorSpec | None:
    item = match_vendor_by_domain(domain)
    return SPEC_BY_NAME.get(item.name) if item else None


def match_vendor_spec_by_fingerprint(html: str) -> AtsVendorSpec | None:
    item = match_vendor_by_fingerprint(html)
    return SPEC_BY_NAME.get(item.name) if item else None


def infer_vendor_from_host(host: str) -> str | None:
    item = match_vendor_by_domain(host)
    return item.name if item else None


def parse_ats_page(page: JobRawPage, ats_hints: dict[str, str] | None = None) -> ExtractedJob:
    parsed_url = urlparse(str(page.final_url or "").strip())
    # Scheme-less URLs ("example.com/jobs") parse with an empty netloc; treat
    # the leading path segment as the host in that case.
    domain = (parsed_url.netloc or parsed_url.path.split("/")[0]).strip().lower()
    parser: ParserFn = parse_generic
    matched = match_vendor_spec_by_domain(domain)
    if matched is None:
        matched = match_vendor_spec_by_fingerprint(page.html)
    if matched is not None:
        parser = matched.parser
    return parser(page, ats_hints=ats_hints)


def extract_seed_from_url(url: str, company_hint: str) -> AtsBoardSeed | None:
    raw_url = str(url or "").strip()
    normalized_url = raw_url if "://" in raw_url else f"https://{raw_url}"
    parsed = urlparse(normalized_url)
    host = parsed.netloc.lower()
    spec = match_vendor_spec_by_domain(host)
    legacy_posting = (
        spec is not None and spec.name == "smartrecruiters" and _SMARTRECRUITERS_WWW_POSTING_RE.match(parsed.path)
    )
    if spec is not None and spec.name not in _WWW_BOARD_VENDORS and not legacy_posting and any(
        _is_marketing_vendor_host(host, domain) for domain in spec.domains if domain.count(".") == 1
    ):
        # The vendor's own apex/www site (www.icims.com/products, smartrecruiters.com/hr-software) is marketing,
        # never a tenant board, whatever its path looks like.
        return None

    board_identifier = ""
    if spec and spec.seed_extractor is not None:
        board_identifier = spec.seed_extractor(normalized_url, company_hint) or ""
    elif _greenhouse_seed(normalized_url, company_hint):
        spec = get_vendor_spec("greenhouse")
        board_identifier = _greenhouse_seed(normalized_url, company_hint) or ""

    preserve_case = spec is not None and spec.name in CASE_SENSITIVE_BOARD_VENDORS
    board_identifier = _normalize_board_identifier(board_identifier, preserve_case=preserve_case)
    if spec is None or not board_identifier:
        return None

    return AtsBoardSeed(
        seed_id=f"api_{spec.name}_{board_identifier}",
        # Opaque tokens (Occupop, CandidateManager) never spell the company's name: keep the hint as given.
        company=(str(company_hint or "").strip() if spec.name in _OPAQUE_BOARD_VENDORS else "")
        or _simple_company(company_hint, board_identifier),
        ats=spec.name,
        board_identifier=board_identifier,
        enabled=True,
        priority=200,
        tags=["api-discovered"],
        country_scope="EU",
    )


def is_direct_supported_ats_host(host: str) -> bool:
    return match_vendor_by_domain(host) is not None


def supported_ats_probe_vendors() -> tuple[AtsVendorSpec, ...]:
    probeable = (
        "greenhouse",
        "lever",
        "ashby",
        "workable",
        "personio",
        "recruitee",
        "teamtailor",
        "smartrecruiters",
        "homerun",
        "jobvite",
        "bamboohr",
        "breezy",
        "jazzhr",
        "factorial",
        "pinpoint",
        "comeet",
    )
    return tuple(SPEC_BY_NAME[name] for name in probeable)


__all__ = [
    "AtsVendorSpec",
    "canonical_domain_for_vendor",
    "extract_seed_from_url",
    "get_vendor_spec",
    "infer_vendor_from_host",
    "is_direct_supported_ats_host",
    "iter_vendor_specs",
    "match_vendor_spec_by_domain",
    "match_vendor_spec_by_fingerprint",
    "parse_ats_page",
    "supported_ats_probe_vendors",
]
