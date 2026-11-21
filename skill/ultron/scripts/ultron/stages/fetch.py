"""``fetch``: download candidate pages and extract postings."""

from __future__ import annotations

import functools
import html as html_lib
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import yaml
from bs4 import BeautifulSoup

from ultron.core.ats.board_policy import vendor_domains
from ultron.core.ats.clients.bamboohr import bamboohr_workplace
from ultron.core.ats.clients.occupop import (
    occupop_detail_url,
    occupop_job_from_detail,
    occupop_job_ref,
    occupop_posting_url,
)
from ultron.core.ats.clients.smartrecruiters import smartrecruiters_location, smartrecruiters_workplace
from ultron.core.ats.clients.workable import workable_locations
from ultron.core.data_paths import data_dir
from ultron.core.decision.unit import load_allowed_domains
from ultron.core.discovery.url_filter import canonicalize_url_for_allowlist, domain_of, is_domain_allowed, normalize_url
from ultron.core.extractors.ats_extractors import parse_ats_page
from ultron.core.extractors.ats_extractors.base import ats_canonical_domain, detect_ats_fingerprint
from ultron.core.job_identity import canonical_job_identity
from ultron.core.models import CandidateUrl, ExtractedJob, JobRawPage
from ultron.core.text_clean import clean_text
from ultron.infra.net import HostBlocked, HttpError, RequestBudgetExceeded, URLRejected, VendorBlocked, top_hosts
from ultron.infra.urlpolicy import admit_url
from ultron.stages import http_factory
from ultron.stages.feeds import is_aggregator_host
from ultron.stages.http_factory import client_for_run
from ultron.storage import runs

_GH_JOB_RE = re.compile(r"^/([^/]+)/(?:embed/)?jobs/([0-9]+)(?:/|$)", re.IGNORECASE)
_WORKABLE_BOARD_JOB_RE = re.compile(r"^/([^/]+)/j/([A-Za-z0-9]{6,})(?:/|$)")
_WORKABLE_SHORT_JOB_RE = re.compile(r"^/j/([A-Za-z0-9]{6,})(?:/|$)")
_SPA_BODY_MIN_CHARS = 400
# Consecutive 403/429 responses from one host before HttpClient stops sending it requests. Every response
# counts (HttpClient's retries of a 429 included) and any other status resets the streak, so a rate-limiting
# host trips the breaker after one URL's attempts. Retries, redirect hops and fallback calls are covered too.
HOST_BLOCK_THRESHOLD = http_factory.HOST_BLOCK_THRESHOLD
# Global stop: once at least this many URLs failed to load at all (DNS failures, connection errors, timeouts)
# and they are at least half of the URLs attempted, the network or proxy is assumed down and the rest is skipped.
NAV_FAILURE_STOP_THRESHOLD = 50
NAV_FAILURE_STOP_RATIO = 0.5


def should_stop_on_nav_failures(nav_failures: int, attempted: int) -> bool:
    return nav_failures >= NAV_FAILURE_STOP_THRESHOLD and nav_failures >= NAV_FAILURE_STOP_RATIO * max(attempted, 1)


def _host_is(host: str, domain: str) -> bool:
    """True when ``host`` is ``domain`` or one of its subdomains (label-aware, not a substring test)."""
    host = host.lower().rstrip(".")
    return host == domain or host.endswith("." + domain)


def _html_to_text(raw: str) -> str:
    if not raw:
        return ""
    unescaped = html_lib.unescape(raw)
    soup = BeautifulSoup(unescaped, "html.parser")
    return "\n".join(line.strip() for line in soup.get_text("\n").splitlines() if line.strip())


def _greenhouse_api_fallback(candidate: CandidateUrl, client) -> tuple[ExtractedJob, str] | None:
    parsed = urlsplit(candidate.normalized_url)
    host = (parsed.hostname or "").lower()
    board, job_id = "", ""
    if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
        match = _GH_JOB_RE.match(parsed.path or "")
        if match:
            board, job_id = match.group(1), match.group(2)
    if (not board or not job_id) and str(candidate.source_key or "").lower().startswith("greenhouse:"):
        board = candidate.source_key.split(":", 1)[1].strip()
        gh_jids = parse_qs(parsed.query).get("gh_jid") or parse_qs(urlsplit(candidate.source_url).query).get("gh_jid")
        if gh_jids and gh_jids[0].isdigit():
            job_id = gh_jids[0]
    if not board or not job_id:
        return None
    api_host = "boards-api.eu.greenhouse.io" if _host_is(host, "eu.greenhouse.io") else "boards-api.greenhouse.io"
    api_url = f"https://{api_host}/v1/boards/{board}/jobs/{job_id}"
    try:
        payload = client.request_json(api_url, allowed_domains=("greenhouse.io",))
    except (RequestBudgetExceeded, HttpError, URLRejected, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or not payload.get("title"):
        return None
    loc_obj = payload.get("location")
    location = str(loc_obj.get("name") or "") if isinstance(loc_obj, dict) else ""
    body_text = _html_to_text(str(payload.get("content") or ""))
    apply_url = normalize_url(str(payload.get("absolute_url") or candidate.normalized_url))
    posted_str = str(payload.get("first_published") or payload.get("updated_at") or "").strip() or None
    job = ExtractedJob(
        title=clean_text(payload.get("title") or candidate.ats_title),
        company=clean_text(payload.get("company_name") or candidate.ats_company or board),
        location=clean_text(location or candidate.ats_location),
        remote_text=clean_text(location or candidate.ats_remote_hint),
        body_text=body_text,
        apply_url=apply_url,
        domain=domain_of(apply_url),
        posting_date=posted_str,
    )
    return job, apply_url


def _greenhouse_api_enrich(candidate: CandidateUrl, job: ExtractedJob, client) -> tuple[ExtractedJob, bool]:
    body = (job.body_text or "").strip()
    low_body = body.lower()
    is_gh_host = job.domain == "greenhouse.io" or job.domain.endswith(".greenhouse.io")
    looks_like_form_shell = (
        is_gh_host
        and len(body) < 1600
        and "apply for this job" in low_body
        and ("first name *" in low_body or "indicates a required field" in low_body)
    )
    if not looks_like_form_shell and (len(body) >= _SPA_BODY_MIN_CHARS or (is_gh_host and len(body) >= 80)):
        return job, False
    fallback = _greenhouse_api_fallback(candidate, client)
    if fallback is None:
        return job, False
    api_job, _ = fallback
    api_body = (api_job.body_text or "").strip()
    if not api_body or (not looks_like_form_shell and len(api_body) <= len(body)):
        return job, False
    return job.model_copy(update={
        "title": api_job.title or job.title,
        "location": api_job.location or job.location,
        "remote_text": api_job.remote_text or job.remote_text,
        "body_text": api_job.body_text,
        "posting_date": api_job.posting_date or job.posting_date,
    }), True


def _workable_api_enrich(candidate: CandidateUrl, job: ExtractedJob, client) -> ExtractedJob:
    if len((job.body_text or "").strip()) >= _SPA_BODY_MIN_CHARS:
        return job
    parsed = urlsplit(candidate.normalized_url)
    host = (parsed.hostname or "").lower()
    if not (host == "workable.com" or host.endswith(".workable.com")):
        return job
    board = ""
    shortcode = ""
    m = _WORKABLE_BOARD_JOB_RE.match(parsed.path or "")
    if m:
        board, shortcode = m.group(1), m.group(2)
    else:
        sm = _WORKABLE_SHORT_JOB_RE.match(parsed.path or "")
        if sm and str(candidate.source_key or "").lower().startswith("workable:"):
            board = candidate.source_key.split(":", 1)[1].strip()
            shortcode = sm.group(1)
    if not board or not shortcode:
        return job
    api_url = f"https://apply.workable.com/api/v2/accounts/{board}/jobs/{shortcode}"
    try:
        payload = client.request_json(api_url, allowed_domains=("workable.com",))
    except (RequestBudgetExceeded, HttpError, URLRejected, ValueError, TypeError):
        return job
    if not isinstance(payload, dict):
        return job
    sections = [
        _html_to_text(str(payload.get(k) or ""))
        for k in ("description", "requirements", "benefits")
    ]
    body_text = "\n\n".join(s for s in sections if s)
    if not body_text:
        return job
    location = "; ".join(workable_locations(payload))
    workplace = str(payload.get("workplace") or "").strip().lower()
    remote_text = job.remote_text
    if workplace == "hybrid":
        remote_text = "Hybrid"
    elif payload.get("remote") is True or workplace == "remote":
        remote_text = f"Remote {location}".strip()
    return job.model_copy(update={
        "title": clean_text(payload.get("title") or job.title),
        "location": clean_text(location or job.location),
        "remote_text": clean_text(remote_text),
        "body_text": body_text,
        "posting_date": str(payload.get("published") or job.posting_date or "").strip() or None,
    })


def _workable_closed_redirect(url: str, final_url: str) -> bool:
    original = urlsplit(url)
    final = urlsplit(final_url)
    return (
        original.hostname == final.hostname == "apply.workable.com"
        and bool(_WORKABLE_BOARD_JOB_RE.match(original.path) or _WORKABLE_SHORT_JOB_RE.match(original.path))
        and parse_qs(final.query).get("not_found") == ["true"]
    )


def _greenhouse_closed_redirect(url: str, final_url: str) -> bool:
    """A Greenhouse job URL that redirected to its board with ``?error=true`` (closed or removed job)."""
    original = urlsplit(url)
    final = urlsplit(final_url)
    return (
        _host_is(original.hostname or "", "greenhouse.io")
        and _host_is(final.hostname or "", "greenhouse.io")
        and bool(_GH_JOB_RE.match(original.path))
        and parse_qs(final.query).get("error") == ["true"]
    )


_SR_JOB_RE = re.compile(r"^/([^/]+)/([A-Za-z0-9_-]{6,})(?:/|$)")
_BAMBOO_JOB_RE = re.compile(r"^/careers/([0-9]+)(?:/|$)", re.IGNORECASE)
_GENERIC_PORTAL_TITLES = frozenset({"join us", "careers", "jobs", "career", "open roles"})
_WD_LOCALE_RE = re.compile(r"^[a-z]{2}(?:-[A-Z]{2})?$")


def _dict_or_empty(value: object) -> object:
    """``{}`` for a missing (``None``) JSON object; every other value is returned for validation."""
    return {} if value is None else value


def _smartrecruiters_api_lookup(
    candidate: CandidateUrl, job: ExtractedJob, client
) -> tuple[ExtractedJob, bool, bool]:
    """``(job, enriched, described)``: ``described`` is True when the API supplied a description."""
    src = str(candidate.source_key or "").strip()
    is_sr_domain = job.domain == "smartrecruiters.com" or job.domain.endswith(".smartrecruiters.com")
    has_posting_url = is_sr_domain and _SR_JOB_RE.match(urlsplit(job.apply_url).path) is not None
    low_title = (job.title or "").strip().lower()
    looks_generic = low_title in _GENERIC_PORTAL_TITLES or low_title.startswith("careers at ")
    if len((job.body_text or "").strip()) >= _SPA_BODY_MIN_CHARS and has_posting_url and not looks_generic:
        return job, False, False
    candidate_host = urlsplit(candidate.normalized_url).hostname or ""
    if not (src.lower().startswith("smartrecruiters:") or _host_is(candidate_host, "smartrecruiters.com")):
        return job, False, False
    parsed = urlsplit(candidate.normalized_url)
    board, job_id = "", ""
    m = _SR_JOB_RE.match(parsed.path or "")
    if m:
        board, job_id = m.group(1), m.group(2)
    if src.lower().startswith("smartrecruiters:"):
        board = src.split(":", 1)[1].strip() or board
    job_id = job_id.split("-")[0] if "-" in job_id else job_id
    if not board or not job_id:
        return job, False, False
    api_url = f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{job_id}"
    try:
        payload = client.request_json(api_url, allowed_domains=("smartrecruiters.com",))
    except (RequestBudgetExceeded, HttpError, URLRejected, ValueError, TypeError):
        return job, False, False
    if not isinstance(payload, dict) or not payload.get("name"):
        return job, False, False
    # Only a missing (null) object defaults to {}; any other non-dict, even an empty list, is malformed.
    job_ad = _dict_or_empty(payload.get("jobAd"))
    sections_dict = _dict_or_empty(job_ad.get("sections")) if isinstance(job_ad, dict) else None
    company_obj = _dict_or_empty(payload.get("company"))
    if not isinstance(sections_dict, dict) or not isinstance(company_obj, dict):
        return job, False, False
    ordered = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")
    parts = [
        _html_to_text(str((sections_dict.get(k) or {}).get("text") or ""))
        for k in ordered
        if isinstance(sections_dict.get(k), dict)
    ]
    body_text = "\n\n".join(p for p in parts if p)
    loc_obj = payload.get("location")
    loc_str = smartrecruiters_location(loc_obj)
    company_name = str(company_obj.get("name") or candidate.ats_company or job.company).strip()
    workplace = smartrecruiters_workplace(loc_obj)
    if not workplace and "remote" in str(payload.get("workplace") or "").lower():
        workplace = "Remote"
    remote_text = f"{workplace} {loc_str}".strip() if workplace else job.remote_text
    # The careers subdomain can redirect a posting to the employer's homepage.
    apply_url = job.apply_url
    if not has_posting_url or looks_generic:
        apply_url = f"https://jobs.smartrecruiters.com/{board}/{job_id}"
    return job.model_copy(update={
        "apply_url": apply_url,
        "domain": domain_of(apply_url),
        "title": clean_text(payload.get("name") or candidate.ats_title or job.title),
        "company": clean_text(company_name),
        "location": clean_text(loc_str or candidate.ats_location or job.location),
        "remote_text": clean_text(remote_text),
        "body_text": body_text or job.body_text,
        "posting_date": str(payload.get("releasedDate") or job.posting_date or "").strip() or None,
    }), True, bool(body_text)


def _smartrecruiters_api_enrich(candidate: CandidateUrl, job: ExtractedJob, client) -> tuple[ExtractedJob, bool]:
    job, enriched, _described = _smartrecruiters_api_lookup(candidate, job, client)
    return job, enriched


def _workday_cxs_detail_url(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if not host.endswith((".myworkdayjobs.com", ".myworkdaysite.com")):
        return None
    segments = [seg for seg in (parsed.path or "").split("/") if seg]
    if "job" not in segments:
        return None
    job_idx = segments.index("job")
    prefix = segments[:job_idx]
    if prefix and _WD_LOCALE_RE.match(prefix[0]):
        prefix = prefix[1:]
    if not prefix:
        return None
    site = prefix[-1]
    tenant = host.split(".")[0]
    rest = "/".join(segments[job_idx:])
    return f"https://{host}/wday/cxs/{tenant}/{site}/{rest}"


def _workday_api_enrich(candidate: CandidateUrl, job: ExtractedJob, client) -> tuple[ExtractedJob, bool]:
    body = (job.body_text or "").strip()
    if len(body) >= _SPA_BODY_MIN_CHARS:
        return job, False
    cxs_url = _workday_cxs_detail_url(candidate.normalized_url) or _workday_cxs_detail_url(job.apply_url)
    if not cxs_url:
        return job, False
    try:
        payload = client.request_json(cxs_url, allowed_domains=("myworkdayjobs.com", "myworkdaysite.com"))
    except (RequestBudgetExceeded, HttpError, URLRejected, ValueError, TypeError):
        return job, False
    info = payload.get("jobPostingInfo") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        return job, False
    desc = _html_to_text(str(info.get("jobDescription") or ""))
    if len(desc) <= len(body):
        return job, False
    remote_type = str(info.get("remoteType") or "").strip()
    location = str(info.get("location") or candidate.ats_location or job.location).strip()
    company = candidate.ats_company or job.company
    return job.model_copy(update={
        "title": clean_text(info.get("title") or candidate.ats_title or job.title),
        "company": clean_text(company),
        "location": clean_text(location),
        "remote_text": clean_text(f"{remote_type} {location}".strip() if remote_type else job.remote_text),
        "body_text": desc,
        "posting_date": str(info.get("startDate") or info.get("postedOn") or job.posting_date or "").strip() or None,
    }), True


def _bamboohr_api_enrich(candidate: CandidateUrl, job: ExtractedJob, client) -> tuple[ExtractedJob, bool]:
    body = (job.body_text or "").strip()
    if len(body) >= _SPA_BODY_MIN_CHARS:
        return job, False
    for raw_url in (candidate.normalized_url, job.apply_url):
        parsed = urlsplit(raw_url)
        host = (parsed.hostname or "").lower()
        if not (host == "bamboohr.com" or host.endswith(".bamboohr.com")):
            continue
        match = _BAMBOO_JOB_RE.match(parsed.path or "")
        if not match:
            continue
        detail_url = f"https://{host}/careers/{match.group(1)}/detail"
        try:
            payload = client.request_json(detail_url, allowed_domains=("bamboohr.com",))
        except (RequestBudgetExceeded, HttpError, URLRejected, ValueError, TypeError):
            return job, False
        result = payload.get("result") if isinstance(payload, dict) else None
        opening = result.get("jobOpening") if isinstance(result, dict) else None
        if not isinstance(opening, dict):
            return job, False
        desc = _html_to_text(str(opening.get("description") or ""))
        if len(desc) <= len(body):
            return job, False
        loc_obj = opening.get("location") if isinstance(opening.get("location"), dict) else {}
        loc_str = ", ".join(
            str(loc_obj.get(k) or "").strip()
            for k in ("city", "state", "addressCountry")
            if loc_obj.get(k)
        ).strip(", ")
        # The vendor's workplace type is authoritative: prose such as "some remote workers" must not make
        # an on-site posting look remote.
        remote_parts = (bamboohr_workplace(opening), job.remote_text or loc_str)
        remote_text = " | ".join(dict.fromkeys(clean_text(part) for part in remote_parts if clean_text(part)))
        return job.model_copy(update={
            "title": clean_text(opening.get("jobOpeningName") or candidate.ats_title or job.title),
            "location": clean_text(loc_str or candidate.ats_location or job.location),
            "remote_text": remote_text,
            "body_text": desc,
            "posting_date": str(opening.get("datePosted") or job.posting_date or "").strip() or None,
        }), True
    return job, False


def get_posting_page(client, url: str) -> tuple[int, str, str]:
    """``(status, final_url, body)`` of a posting. An Occupop posting link answers a redirect to a script-only page
    on another host, so its body is the vendor's job JSON instead (requested on the vendor's domain only)."""
    if ref := occupop_job_ref(url):
        return client.get_text(occupop_detail_url(ref), allowed_domains=vendor_domains("occupop"))
    return client.get_text(url)


def occupop_posting_job(url: str, body: str, *, company_hint: str = "") -> tuple[ExtractedJob | None, str]:
    """``(job, "")`` from the Occupop job JSON fetched by :func:`get_posting_page`, or ``(None, reason)``."""
    try:
        payload = json.loads(body or "")
    except ValueError:
        return None, "extract_failed: invalid Occupop job JSON"
    job = occupop_job_from_detail(payload, occupop_posting_url(occupop_job_ref(url)), company_hint=company_hint)
    if job is None:  # {"job": null}, or a job marked closed or inactive
        return None, "expired_or_closed"
    return job, ""


def _lane(candidate: CandidateUrl) -> str:
    """Round-robin lane: the ATS board or feed (``source_key``); web-search hits get one lane per submitted query."""
    if candidate.source_type == "web_search" or not candidate.source_key:
        query = " ".join(str(candidate.query or "").split()).casefold()
        return f"web_search:{query}" if query else "web_search"
    return candidate.source_key


def _round_robin(items: list[tuple[CandidateUrl, str]]) -> list[tuple[CandidateUrl, str]]:
    """One candidate per lane in turn (first-seen lane order), keeping within-lane order."""
    groups: dict[str, list[tuple[CandidateUrl, str]]] = {}
    for item in items:
        groups.setdefault(_lane(item[0]), []).append(item)
    queues = list(groups.values())
    out: list[tuple[CandidateUrl, str]] = []
    for rank in range(max((len(q) for q in queues), default=0)):
        out.extend(q[rank] for q in queues if rank < len(q))
    return out


def page_canonical_url(html: str, base_url: str) -> str | None:
    """The page's ``<link rel="canonical">`` resolved against ``base_url``; ``None`` unless it is an http(s) URL."""
    if not html or "canonical" not in html.lower():
        return None
    link = BeautifulSoup(html, "html.parser").find("link", attrs={"rel": "canonical"}, href=True)
    href = str(link.get("href") or "").strip() if link is not None else ""
    if not href:
        return None
    try:
        return admit_url(urljoin(base_url, href)).url  # http(s) only, no credentials, no private IP literal
    except (URLRejected, ValueError):
        return None


@functools.cache
def _blocked_final_domains() -> tuple[str, ...]:
    rules = yaml.safe_load((data_dir() / "domain_rules.yaml").read_text(encoding="utf-8")) or {}
    return tuple(str(domain) for domain in rules.get("blocked_final_domains") or [])


def _allowlist_url(raw_url: str, candidate: CandidateUrl, allowed_domains: list[str]) -> str:
    """``raw_url`` normalized; a custom-domain ``?gh_jid=`` URL of a known Greenhouse board becomes its board URL."""
    source_key = str(candidate.source_key or "")
    board_label = f"[{source_key}]" if source_key.lower().startswith("greenhouse:") else ""
    return canonicalize_url_for_allowlist(raw_url, board_label, allowed_domains)


def resolve_apply_url(
    candidate: CandidateUrl, final_url: str, canonical_url: str | None, allowed_domains: list[str]
) -> str:
    """First of final URL, rel=canonical and candidate URL that lies on the ATS allowlist; else the final URL.

    A final URL on a blocked or aggregator host is kept as is, so the filter still discards it.

    The canonical link is page-controlled, so it only counts when it names a vendor job (not a board root, which
    would merge every posting of the board into one identity).
    """
    final_host = domain_of(normalize_url(final_url))
    if is_aggregator_host(final_host) or is_domain_allowed(final_host, list(_blocked_final_domains())):
        return normalize_url(final_url)  # stays discardable by the filter, whatever the page links to
    for raw_url, page_controlled in ((final_url, False), (canonical_url, True), (candidate.normalized_url, False)):
        if not raw_url:
            continue
        url = _allowlist_url(raw_url, candidate, allowed_domains)
        if not is_domain_allowed(domain_of(url), allowed_domains):
            continue
        if page_controlled and canonical_job_identity(url).identity_key.startswith(("url:", "raw:")):
            continue
        return url
    return _allowlist_url(final_url, candidate, allowed_domains)


def has_ats_fingerprint(html: str) -> bool:
    """MARK III ``verification_fetch`` condition: a vendor fingerprint with a known canonical domain."""
    vendor = detect_ats_fingerprint(html or "")
    return bool(vendor and ats_canonical_domain(vendor))


# Thresholds stay low so short real postings are kept; a false positive would drop a posting.
_CHALLENGE_MAX_VISIBLE_CHARS = 1000
_SHELL_MAX_VISIBLE_CHARS = 120
# Whole words only: "recaptcha" badges on real forms, "captchaless" or "security checker" do not count.
_CHALLENGE_RE = re.compile(r"(?<![a-z])(?:captchas?|verify you are human|just a moment|checking your browser)(?![a-z])")
# "security check" also appears in real postings ("applicants must pass a security check"), so it only counts
# together with browser-challenge wording.
_SECURITY_CHECK_RE = re.compile(r"(?<![a-z])security check(?![a-z])")
_CHALLENGE_CONTEXT_RE = re.compile(r"(?<![a-z])(?:browser|javascript|cookies|cloudflare|ray id|robot)(?![a-z])")
# A "Client Challenge" interstitial asks to enable JavaScript; "client challenge" alone is ordinary posting prose.
_CLIENT_CHALLENGE_RE = re.compile(r"(?<![a-z])client challenge(?![a-z])")
_JAVASCRIPT_RE = re.compile(r"(?<![a-z])javascript(?![a-z])")
_SCRIPT_TAG_RE = re.compile(r"<script\b", re.IGNORECASE)


def unusable_page_reason(html: str, job: ExtractedJob) -> str | None:
    """``blocked`` for a short bot-challenge page, ``js_shell`` for a script-rendered page with almost no text."""
    body = (job.body_text or "").strip()
    if len(body) >= _CHALLENGE_MAX_VISIBLE_CHARS:
        return None
    visible = _html_to_text(html)
    low = " ".join(visible.lower().split())  # text nodes are joined with newlines: "verify you are\nhuman"
    if len(visible) < _CHALLENGE_MAX_VISIBLE_CHARS and (
        _CHALLENGE_RE.search(low)
        or (_SECURITY_CHECK_RE.search(low) and _CHALLENGE_CONTEXT_RE.search(low))
        or (_CLIENT_CHALLENGE_RE.search(low) and _JAVASCRIPT_RE.search(low))
    ):
        return "blocked"
    shell = len(visible) < _SHELL_MAX_VISIBLE_CHARS and len(body) < _SHELL_MAX_VISIBLE_CHARS
    # A recognized ATS embed is still useful downstream (fingerprint, ATS hints), so only unknown shells are dropped.
    if shell and _SCRIPT_TAG_RE.search(html) and not has_ats_fingerprint(html):
        return "js_shell"
    return None


def fetch(run_id: str) -> dict:
    runs.require_stage(run_id, "fetch")
    run = runs.load_run(run_id)
    out_dir = runs.run_dir(run_id)
    max_pages = run["settings"]["max_pages"]
    # Per-host caps limit seeds per API host in discovery, not page fetching: shared ATS hosts are bounded by max_pages.
    client = client_for_run(run, max_requests_per_host=max_pages, block_threshold=HOST_BLOCK_THRESHOLD)
    allowed_domains = load_allowed_domains()

    unique: list[tuple[CandidateUrl, str]] = []
    seen: set[str] = set()
    for row in runs.read_jsonl(out_dir / "candidates.jsonl"):
        candidate = CandidateUrl.model_validate(row)
        identity = canonical_job_identity(
            candidate.source_url, source_type=candidate.source_type, source_key=candidate.source_key
        ).identity_key
        if identity in seen:
            continue
        seen.add(identity)
        unique.append((candidate, identity))

    ordered = _round_robin(unique)
    capped: list[tuple[CandidateUrl, str]] = []
    # Page slots: every candidate takes one, except those skipped without a request because their ATS vendor is
    # blocked or out of budget for this run; their slot goes to the next candidate instead of being wasted.
    slots = 0
    postings: list[dict] = []
    errors: list[dict] = []
    skipped_hosts: set[str] = set()
    skipped_vendors: set[str] = set()
    budget_spent = nav_stopped = False
    attempted = nav_failures = 0
    for candidate, identity in ordered:
        url = candidate.normalized_url
        if slots >= max_pages:
            capped.append((candidate, identity))
            continue
        slots += 1
        if nav_stopped:
            errors.append({"url": url, "error": "skipped_navigation_failures"})
            continue
        if budget_spent:
            errors.append({"url": url, "error": "skipped_request_budget_exhausted"})
            continue
        if candidate.domain in skipped_hosts:
            errors.append({"url": url, "error": "skipped_host_budget_exhausted"})
            continue
        if (vendor := client.vendor_for(candidate.domain)) in skipped_vendors:
            errors.append({"url": url, "error": "skipped_vendor_budget_exhausted", "vendor": vendor})
            slots -= 1
            continue
        attempted += 1
        occupop_ref = occupop_job_ref(url)
        try:
            status, final_url, html = get_posting_page(client, url)
        except VendorBlocked as exc:
            # Any tenant of this vendor answered 429/403 too often in this run: no request was sent.
            errors.append({"url": url, "error": "vendor_blocked", "vendor": exc.vendor})
            slots -= 1
            attempted -= 1  # no request was sent: not an attempt for the navigation-failure stop
            continue
        except HostBlocked:
            attempted -= 1
            if (fallback := _greenhouse_api_fallback(candidate, client)) is not None:  # different (API) host
                postings.append({"candidate": candidate.model_dump(mode="json"), "job": fallback[0].model_dump(mode="json"),
                                 "identity_key": identity, "ats_fingerprint": True})
                continue
            errors.append({"url": url, "error": "host_blocked"})
            continue
        except RequestBudgetExceeded as exc:
            attempted -= 1  # refused before sending
            if exc.scope == "host":
                skipped_hosts.add(exc.host or candidate.domain)
                errors.append({"url": url, "error": f"host_budget_exhausted: {exc.host}"})
            elif exc.scope == "vendor":
                skipped_vendors.add(exc.vendor)
                errors.append({"url": url, "error": f"vendor_budget_exhausted: {exc.vendor}", "vendor": exc.vendor})
                slots -= 1
            else:
                budget_spent = True
                errors.append({"url": url, "error": "request_budget_exhausted"})
            continue
        except HttpError as exc:
            fallback = _greenhouse_api_fallback(candidate, client)
            if fallback is not None:
                job, _ = fallback
                postings.append({
                    "candidate": candidate.model_dump(mode="json"),
                    "job": job.model_dump(mode="json"),
                    "identity_key": identity,
                    "ats_fingerprint": True,
                })
                continue
            errors.append({"url": url, "error": f"http_error: {exc}", "status": exc.status_code})
            if exc.status_code is None:  # transport failure: no HTTP response at all
                nav_failures += 1
                nav_stopped = should_stop_on_nav_failures(nav_failures, attempted)
            continue
        except URLRejected as exc:
            errors.append({"url": url, "error": f"url_rejected: {exc}"})
            if str(exc) == "dns_resolution_failed":  # with no network, name resolution fails first
                nav_failures += 1
                nav_stopped = should_stop_on_nav_failures(nav_failures, attempted)
            continue
        if status >= 400:
            if status in {403, 429} or status >= 500:
                fallback = _greenhouse_api_fallback(candidate, client)
                if fallback is not None:
                    job, _ = fallback
                    postings.append({
                        "candidate": candidate.model_dump(mode="json"),
                        "job": job.model_dump(mode="json"),
                        "identity_key": identity,
                        "ats_fingerprint": True,
                    })
                    continue
            reason = "expired_or_closed" if status in {404, 410} else "inaccessible"
            errors.append({"url": url, "error": reason, "status": status})
            continue
        if _workable_closed_redirect(url, final_url) or _greenhouse_closed_redirect(url, final_url):
            errors.append({"url": url, "error": "expired_or_closed", "status": status})
            continue
        if occupop_ref:
            job, reason = occupop_posting_job(url, html, company_hint=candidate.ats_company)
            if job is None:
                errors.append({"url": url, "error": reason, "status": status})
                continue
            postings.append({"candidate": candidate.model_dump(mode="json"), "job": job.model_dump(mode="json"),
                             "identity_key": identity, "ats_fingerprint": True})
            continue
        page = JobRawPage(url=url, final_url=final_url, status_code=status, html=html,
                          canonical_url=page_canonical_url(html, final_url))
        # Same hint keys and sources as MARK III's verification_fetch.
        ats_hints = {
            "title": candidate.ats_title,
            "company": candidate.ats_company,
            "location": candidate.ats_location,
            "remote_hint": candidate.ats_remote_hint,
        }
        try:
            job = parse_ats_page(page, ats_hints=ats_hints)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append({"url": url, "error": f"extract_failed: {exc!r}"})
            continue
        job.title, job.company, job.location = clean_text(job.title), clean_text(job.company), clean_text(job.location)
        job.apply_url = normalize_url(final_url)
        job.domain = domain_of(job.apply_url)
        job, gh_enriched = _greenhouse_api_enrich(candidate, job, client)
        workable_job = _workable_api_enrich(candidate, job, client)
        workable_enriched, job = workable_job is not job, workable_job
        # SmartRecruiters metadata without a description keeps the page text, which still needs classifying.
        job, sr_enriched, sr_described = _smartrecruiters_api_lookup(candidate, job, client)
        job, wd_enriched = _workday_api_enrich(candidate, job, client)
        job, bamboo_enriched = _bamboohr_api_enrich(candidate, job, client)
        enriched = gh_enriched or workable_enriched or sr_described or wd_enriched or bamboo_enriched
        if job.apply_url == normalize_url(final_url):  # an enricher's posting URL is kept as is
            job.apply_url = resolve_apply_url(candidate, final_url, page.canonical_url, allowed_domains)
            job.domain = domain_of(job.apply_url)
        if not enriched and (reason := unusable_page_reason(html, job)):
            errors.append({"url": url, "error": reason, "status": status})  # left for the agent to open manually
            continue
        postings.append({
            "candidate": candidate.model_dump(mode="json"),
            "job": job.model_dump(mode="json"),
            "identity_key": identity,
            "ats_fingerprint": gh_enriched or sr_enriched or wd_enriched or bamboo_enriched or has_ats_fingerprint(html),
        })

    # Written even when empty, so ``diagnose`` can tell a capped candidate from one added after fetch ran.
    runs.write_jsonl(out_dir / "fetch_capped.jsonl", [
        {"url": candidate.normalized_url, "identity_key": identity, "source_key": candidate.source_key,
         "error": "max_pages_limit"}
        for candidate, identity in capped
    ])
    runs.write_jsonl(out_dir / "postings.jsonl", postings)
    runs.write_jsonl(out_dir / "fetch_errors.jsonl", errors)
    runs.mark_stage(run_id, "fetch")
    summary = {"fetched": len(postings), "failed": len(errors), "capped": len(capped),
               "navigation_failures": nav_failures, "requests": client.requests_made,
               "requests_by_domain": top_hosts(client.requests_by_host()),
               "requests_by_vendor": dict(sorted(client.requests_by_vendor().items())),
               "vendors_blocked": client.vendors_blocked()}
    if nav_stopped:
        summary["stopped"] = "navigation_failures"
    return summary
