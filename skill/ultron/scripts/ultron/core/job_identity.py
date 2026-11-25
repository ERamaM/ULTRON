"""Stable, conservative identity for job-posting URLs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_TRACKING_KEYS = frozenset(
    {
        "campaign",
        "fbclid",
        "gclid",
        "gh_src",
        "lever-source",
        "ref",
        "referer",
        "referrer",
        "source",
        "trk",
        "tracking",
        # Ad click IDs and email/marketing-automation parameters.
        "_gl",
        "_hsenc",
        "_hsmi",
        "dclid",
        "gbraid",
        "igshid",
        "lever-origin",
        "li_fat_id",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "msclkid",
        "ttclid",
        "twclid",
        "wbraid",
        "yclid",
        # Matomo/Piwik campaign keys (listed explicitly: a job board could use another pk_ key as an id).
        "pk_campaign",
        "pk_cid",
        "pk_content",
        "pk_cpn",
        "pk_keyword",
        "pk_kwd",
        "pk_medium",
        "pk_source",
    }
)
_TRACKING_PREFIXES = ("utm_", "hsa_", "mtm_")
_JOB_ID_QUERY_KEY_PRIORITY = (
    "gh_jid",
    "jobid",
    "job_id",
    "jid",
    "rid",
    "wdi",
    "positionid",
    "requisitionid",
    "job",
    "id",
)
_JOB_ID_QUERY_KEYS = frozenset(_JOB_ID_QUERY_KEY_PRIORITY)


@dataclass(frozen=True, slots=True)
class CanonicalJobIdentity:
    """Stable deduplication identity derived from an ATS vendor ID or URL."""
    identity_key: str
    canonical_url: str
    fallback_reason: str | None = None


def _is_tracking_key(key: str) -> bool:
    low = key.casefold()
    return low.startswith(_TRACKING_PREFIXES) or low in _TRACKING_KEYS


def _source_vendor(source_type: str, source_key: str) -> tuple[str, str]:
    source_type_low = str(source_type or "").strip().casefold()
    source_key_low = str(source_key or "").strip().casefold()
    for vendor in ("greenhouse", "lever", "ashby", "workable", "workday", "personio", "recruitee"):
        if source_key_low.startswith(f"{vendor}:"):
            return vendor, source_key_low.split(":", 1)[1].strip()
        if source_type_low == vendor:
            return vendor, source_key_low
    return "", ""


def _fallback(raw: str, reason: str) -> CanonicalJobIdentity:
    value = raw.strip().casefold()
    logger.warning("job_identity_fallback reason=%s url=%r", reason, raw[:240])
    return CanonicalJobIdentity(identity_key=f"raw:{value}", canonical_url=value, fallback_reason=reason)


def _normalized_host(parsed) -> tuple[str, str]:
    host = parsed.hostname
    if not host:
        raise ValueError("missing_host")
    ascii_host = host.rstrip(".").encode("idna").decode("ascii").casefold()
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid_port") from exc
    default_port = (parsed.scheme.casefold() == "http" and port == 80) or (
        parsed.scheme.casefold() == "https" and port == 443
    )
    rendered_host = f"[{ascii_host}]" if ":" in ascii_host else ascii_host
    netloc = rendered_host if port is None or default_port else f"{rendered_host}:{port}"
    return ascii_host, netloc


def _query_pairs(query: str) -> list[tuple[str, str]]:
    pairs = [(key, value) for key, value in parse_qsl(query, keep_blank_values=True) if not _is_tracking_key(key)]
    return sorted(pairs, key=lambda item: (item[0].casefold(), item[1]))


def _query_job_id(pairs: list[tuple[str, str]]) -> str:
    values: dict[str, str] = {}
    for key, value in pairs:
        folded = key.casefold()
        if folded in _JOB_ID_QUERY_KEYS and value.strip() and folded not in values:
            values[folded] = value.strip().casefold()
    for key in _JOB_ID_QUERY_KEY_PRIORITY:
        if key in values:
            return values[key]
    return ""


def _host_matches(host: str, domain: str) -> bool:
    normalized = str(host or "").casefold().rstrip(".")
    suffix = str(domain or "").casefold().lstrip(".")
    return normalized == suffix or normalized.endswith(f".{suffix}")


def _tenant(host: str, domain: str) -> str:
    """Labels left of ``domain`` in ``host`` (``acme`` for acme.teamtailor.com); empty for the apex or www host."""
    if not host.endswith(f".{domain}"):
        return ""
    tenant = host[: -len(domain) - 1]
    return "" if tenant in {"", "www"} else tenant


def _pair_value(pairs: list[tuple[str, str]], key: str) -> str:
    return next((value.strip() for name, value in pairs if name.casefold() == key and value.strip()), "")


# A Comeet company or position UID, such as 41.001 or C7.E11.
_COMEET_UID_RE = re.compile(r"^[0-9a-f]{2}\.[0-9a-f]{2,3}$")
_UUID_RE = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"


def _hosted_vendor_identity(host: str, path: str, pairs: list[tuple[str, str]]) -> str:
    """Identity for vendors recognized by their hosted board domain only (never by source key or custom domain).

    Each rule requires a job ID that the vendor puts in the URL; boards, search pages and slug-only URLs keep the
    URL identity. Jobvite and JazzHR IDs are case-sensitive, so their case is kept.
    """
    low_path = path.casefold()
    if host in {"jobs.smartrecruiters.com", "careers.smartrecruiters.com"}:
        match = re.match(r"/([^/]+)/([0-9]{6,})(?:-[^/]*)?(?:/|$)", low_path)
        if match:
            return f"smartrecruiters:{match.group(1)}:{match.group(2)}"
    if tenant := _tenant(host, "teamtailor.com"):
        match = re.match(r"(?:/[a-z]{2}(?:-[a-z]{2})?)?/jobs/([0-9]+)(?:-[^/]*)?(?:/|$)", low_path)
        if match:
            return f"teamtailor:{tenant}:{match.group(1)}"
    if tenant := _tenant(host, "recruitee.com"):
        match = re.match(r"/o/([a-z0-9][a-z0-9-]*)(?:/|$)", low_path)
        if match:
            return f"recruitee:{tenant}:{match.group(1)}"
    if tenant := _tenant(host, "breezy.hr"):
        match = re.match(r"/p/([0-9a-f]{8,})(?:-[^/]*)?(?:/|$)", low_path)
        if match:
            return f"breezy:{tenant}:{match.group(1)}"
    if tenant := _tenant(host, "pinpointhq.com"):
        match = re.match(rf"(?:/[a-z]{{2}}(?:-[a-z]{{2}})?)?/postings/({_UUID_RE})(?:/|$)", low_path)
        if match:
            return f"pinpoint:{tenant}:{match.group(1)}"
    if tenant := _tenant(host, "bamboohr.com"):
        match = re.match(r"/careers/([0-9]+)(?:/|$)", low_path)
        job_id = match.group(1) if match else ""
        if not job_id and low_path == "/jobs/view.php" and _pair_value(pairs, "id").isdigit():
            job_id = _pair_value(pairs, "id")
        if job_id:
            return f"bamboohr:{tenant}:{job_id}"
    if host in {"jobs.jobvite.com", "jobs.eu.jobvite.com"}:
        match = re.match(r"/([^/]+)/job/([A-Za-z0-9]{5,})(?:/|$)", path)
        if match:
            return f"jobvite:{match.group(1).casefold()}:{match.group(2)}"
    if tenant := _tenant(host, "icims.com"):
        match = re.match(r"/jobs/([0-9]+)(?:/|$)", low_path)
        if match:
            return f"icims:{tenant}:{match.group(1)}"
    if tenant := _tenant(host, "taleo.net"):
        job_id = _pair_value(pairs, "job").casefold()
        if low_path.endswith("/jobdetail.ftl") and re.fullmatch(r"[a-z0-9-]+", job_id):
            return f"taleo:{tenant}:{job_id}"
    if host in {"comeet.co", "www.comeet.co", "comeet.com", "www.comeet.com"}:
        # /jobs/{slug}/{companyUid}/{positionUid}/{name} or /jobs/{slug}/{companyUid}/{name}/{positionUid}.
        segments = [segment for segment in low_path.split("/") if segment]
        if len(segments) >= 4 and segments[0] == "jobs" and _COMEET_UID_RE.match(segments[2]):
            position_uids = [segment for segment in segments[3:5] if _COMEET_UID_RE.match(segment)]
            if len(position_uids) == 1:
                return f"comeet:{segments[2]}:{position_uids[0]}"
    if _host_matches(host, "candidatemanager.net") and low_path == "/cm/p/pjobdetails.aspx":
        # pJobDetails.aspx?mid=<mid>&sid=<sid>&jid=<jid>[&a=<token>]: the extra token does not name the vacancy.
        # The ids are opaque and case-sensitive, so their case is kept.
        mid, sid, jid = (_pair_value(pairs, key) for key in ("mid", "sid", "jid"))
        if all(re.fullmatch(r"[A-Za-z0-9]+", value) for value in (mid, sid, jid)):
            return f"candidatemanager:{mid}-{sid}:{jid}"
    if tenant := _tenant(host, "applytojob.com"):
        match = re.match(r"/apply/([A-Za-z0-9]{4,})(?:/|$)", path)
        if match and match.group(1).casefold() != "jobs":
            return f"jazzhr:{tenant}:{match.group(1)}"
    return ""


def _vendor_identity(*, host: str, path: str, pairs: list[tuple[str, str]], source_type: str, source_key: str) -> str:
    vendor, source_board = _source_vendor(source_type, source_key)
    low_path = path.casefold()
    query_id = _query_job_id(pairs)

    if vendor == "greenhouse" or _host_matches(host, "greenhouse.io"):
        path_match = re.search(r"/(?:embed/)?jobs?/([0-9]+)(?:/|$)", low_path)
        job_id = (path_match.group(1) if path_match else "") or query_id
        board_match = re.match(r"/([^/]+)/(?:embed/)?jobs?", low_path)
        board = source_board or (board_match.group(1) if board_match else "") or host
        if not job_id and _host_matches(host, "greenhouse.io") and low_path == "/embed/job_app":
            # Embedded application form: boards.greenhouse.io/embed/job_app?for=<board>&token=<job id>.
            token, embed_board = _pair_value(pairs, "token"), _pair_value(pairs, "for").casefold()
            if token.isdigit():
                job_id, board = token, embed_board or source_board or host
        if job_id:
            return f"greenhouse:{board}:{job_id}"

    if vendor == "lever" or _host_matches(host, "lever.co"):
        match = re.match(r"/([^/]+)/([0-9a-f-]{16,})(?:/|$)", low_path)
        if match:
            return f"lever:{match.group(1)}:{match.group(2)}"

    if vendor == "ashby" or _host_matches(host, "ashbyhq.com"):
        match = re.match(r"/([^/]+)/([0-9a-f-]{16,})(?:/|$)", low_path)
        if match:
            return f"ashby:{match.group(1)}:{match.group(2)}"
    ashby_jid = _pair_value(pairs, "ashby_jid").casefold()
    if re.fullmatch(r"[0-9a-f-]{16,}", ashby_jid):  # Ashby board embedded on the employer's own domain
        board = source_board if vendor == "ashby" and source_board else host
        return f"ashby:{board}:{ashby_jid}"

    if vendor == "workable" or _host_matches(host, "workable.com"):
        match = re.match(r"/([^/]+)/j/([a-z0-9]{6,})(?:/|$)", low_path)
        if match:
            return f"workable:{match.group(1)}:{match.group(2)}"
        short = re.match(r"/j/([a-z0-9]{6,})(?:/|$)", low_path)
        if short:
            board = source_board or host
            return f"workable:{board}:{short.group(1)}"

    if vendor == "workday" or _host_matches(host, "myworkdayjobs.com"):
        # A repost suffix (_R-2048-1) is kept: it is a separate Workday posting, and dropping it would also merge
        # distinct requisitions such as JR-2024-1 and JR-2024-2.
        requisitions = re.findall(r"_([a-z]{1,3}-?[0-9]+(?:-[0-9]{1,3})?)(?:/|$)", low_path)
        job_id = (requisitions[-1] if requisitions else "") or query_id
        if job_id:
            return f"workday:{host}:{job_id}"

    if vendor == "recruitee" and source_board:
        # A Recruitee board listed through its API on the employer's own domain (jobs.example.com/o/<slug>) is the
        # same posting as <board>.recruitee.com/o/<slug>.
        match = re.match(r"/o/([a-z0-9][a-z0-9-]*)(?:/|$)", low_path)
        if match:
            return f"recruitee:{source_board}:{match.group(1)}"

    if vendor == "personio" or _host_matches(host, "personio.com") or _host_matches(host, "personio.de"):
        match = re.search(r"/(?:job|position)/([a-z0-9-]+)(?:/|$)", low_path)
        job_id = (match.group(1) if match else "") or query_id
        if job_id:
            tenant = source_board or host.split(".", 1)[0]
            return f"personio:{tenant}:{job_id}"

    return _hosted_vendor_identity(host, path, pairs)


def canonical_job_identity(url: str, *, source_type: str = "", source_key: str = "") -> CanonicalJobIdentity:
    """Return a canonical URL and stable identity without dropping unknown ID parameters."""

    raw = str(url or "").strip()
    if not raw:
        return _fallback(raw, "empty_url")
    candidate = raw if re.match(r"^[a-z][a-z0-9+.-]*:", raw, re.IGNORECASE) else f"https://{raw.lstrip('/')}"
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.casefold()
        if scheme not in {"http", "https"}:
            return _fallback(raw, "unsupported_scheme")
        host, netloc = _normalized_host(parsed)
        path = parsed.path or ""
        if path != "/":
            path = path.rstrip("/")
        pairs = _query_pairs(parsed.query)
        query = urlencode(pairs, doseq=True)
        canonical_url = urlunsplit((scheme, netloc, path, query, ""))
        vendor_key = _vendor_identity(
            host=host,
            path=path,
            pairs=pairs,
            source_type=source_type,
            source_key=source_key,
        )
        url_key = urlunsplit(("", netloc, path, query, ""))
        return CanonicalJobIdentity(identity_key=vendor_key or f"url:{url_key}", canonical_url=canonical_url)
    except (UnicodeError, ValueError) as exc:
        return _fallback(raw, str(exc) or type(exc).__name__)


__all__ = ["CanonicalJobIdentity", "canonical_job_identity"]
