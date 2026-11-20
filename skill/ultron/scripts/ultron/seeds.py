"""Seed inventory (auto seeds + bundled packs) and on-the-fly company verification.

Seed identity: ``seed_id == f"{ats}:{board}"`` where ``(ats, board)`` is
``ultron.core.ats.identity.seed_identity`` (vendor and board lowercased, Workday keeps
its case-sensitive site path). The same pair is the dedup key.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

from ultron import home
from ultron.core.ats.clients import AtsClientError, fetch_vendor_jobs
from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import GENERIC_BOARD_IDENTIFIERS, extract_seed_from_url, is_direct_supported_ats_host
from ultron.core.ats.vendors import SUPPORTED_ATS_DOMAINS
from ultron.core.feeds.urls import AGGREGATOR_DOMAINS, fetch_page_links, looks_like_company_jobs_url
from ultron.core.models import AtsBoardSeed
from ultron.core.sectors import seed_sector_values
from ultron.net import HttpClient, HttpError, RequestBudgetExceeded, URLRejected

logger = logging.getLogger(__name__)

# Vendors probed by slug, in order. The bare-slug vendors added last (jobvite, bamboohr, jazzhr, factorial) cost
# about 2, 2, 1 and 2 requests per missed slug. The rest (workday, icims, taleo need a host; comeet needs a
# company UID next to the slug) are only reached via an explicit ats/board pair or a careers_url.
PROBE_VENDORS = (
    "greenhouse",
    "lever",
    "ashby",
    "workable",
    "recruitee",
    "teamtailor",
    "smartrecruiters",
    "personio",
    "pinpoint",
    "homerun",
    "breezy",
    "jobvite",
    "bamboohr",
    "jazzhr",
    "factorial",
)
_TRUE = {"1", "true", "yes", "y", "on", "si", "sí"}
_FALSE = {"0", "false", "no", "n", "off"}


def make_seed_id(ats: str, board: str) -> str:
    vendor, ident = seed_identity(ats, board)
    return f"{vendor}:{ident}"


def auto_seeds_path() -> Path:
    return home.ultron_home() / "seeds" / "auto_seeds.yaml"


def packs_dir() -> Path:
    return home.data_dir() / "seed_packs"


def _coerce_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return default


def _coerce_int(value: object, default: int) -> int:
    try:
        return default if value is None or str(value).strip() == "" else int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _tags(value: object) -> list[str]:
    raw = value.split(",") if isinstance(value, str) else list(value) if isinstance(value, (list, tuple, set)) else []
    return list(dict.fromkeys(str(t).strip() for t in raw if str(t or "").strip()))


def _parse_rows(rows: list) -> list[AtsBoardSeed]:
    seeds: list[AtsBoardSeed] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ats = str(row.get("ats", "")).strip().lower()
        board = str(row.get("board_identifier", "")).strip()
        try:
            seed_id = make_seed_id(ats, board)
            seed = AtsBoardSeed(
                seed_id=seed_id,
                company=str(row.get("company", "")).strip() or board,
                ats=ats,
                board_identifier=board,
                enabled=_coerce_bool(row.get("enabled", True)),
                priority=_coerce_int(row.get("priority", 100), 100),
                tags=_tags(row.get("tags", [])),
                profiles=row.get("profiles") or [],
                sectors=seed_sector_values(row.get("sectors", []), seed_id=seed_id, logger=logger),
                country_scope=str(row.get("country_scope", "EU")).strip() or "EU",
                source_type=str(row.get("source_type", "manual_yaml")).strip() or "manual_yaml",
                source_label=str(row.get("source_label", "Manual YAML")).strip() or "Manual YAML",
                source_url=str(row.get("source_url", "")).strip(),
                verification=row.get("verification") or {},
            )
        except (ValueError, TypeError) as exc:  # invalid row: skip, never abort the inventory
            logger.warning("Skipping invalid seed row ats=%s board=%s error=%s", ats, board, type(exc).__name__)
            continue
        if seed.company and seed.board_identifier:
            seeds.append(seed)
    return seeds


def _read_yaml_seeds(path: Path) -> list[AtsBoardSeed]:
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        logger.warning("Invalid seed file file=%s", path.name)
        return []
    rows = data.get("seeds", []) if isinstance(data, dict) else []
    return _parse_rows(rows) if isinstance(rows, list) else []


def _read_pack_seeds() -> list[AtsBoardSeed]:
    seeds: list[AtsBoardSeed] = []
    for file in sorted(packs_dir().glob("*.yaml")):
        try:
            data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(data, dict) or (data.get("pack") or {}).get("usage") != "active":
            continue
        rows = data.get("seeds", [])
        if isinstance(rows, list):
            seeds.extend(_parse_rows(rows))
    return seeds


def load_inventory(profile_slug: str | None) -> list[AtsBoardSeed]:
    """Enabled seeds: auto seeds linked to the profile first, then the bundled packs.

    Auto seeds linked only to other profiles are left out. With ``profile_slug=None``
    every auto seed comes first. Dedup key is ``seed_identity`` (first occurrence wins).
    """
    auto = [s for s in _read_yaml_seeds(auto_seeds_path()) if s.enabled]
    if profile_slug is not None:
        auto = [s for s in auto if profile_slug in s.profiles]
    out: dict[tuple[str, str], AtsBoardSeed] = {}
    for seed in [*auto, *(s for s in _read_pack_seeds() if s.enabled)]:
        out.setdefault(seed_identity(seed.ats, seed.board_identifier), seed)
    return list(out.values())


class SeedFileError(RuntimeError):
    """auto_seeds.yaml exists but cannot be read safely."""


def _read_raw_rows(path: Path) -> list:
    if not path.is_file():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        rows = data.get("seeds", []) if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise TypeError("seeds is not a list")
    except (OSError, TypeError, ValueError, yaml.YAMLError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{datetime.now(UTC):%Y%m%dT%H%M%S%f}")
        try:
            shutil.copy2(path, backup)
        except OSError:
            backup = path
        raise SeedFileError(f"{path.name} is unreadable ({type(exc).__name__}); copy kept at {backup}") from exc
    return rows


# Writers hold ``auto_seeds.yaml.lock`` across read-modify-write. A lock older than SEED_LOCK_STALE seconds was
# left by a crashed writer (a write takes milliseconds) and is removed; waiting longer than SEED_LOCK_TIMEOUT
# for a live one raises SeedFileError.
SEED_LOCK_TIMEOUT = 15.0
SEED_LOCK_STALE = 120.0


@contextmanager
def _seed_file_lock(path: Path) -> Iterator[None]:
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + SEED_LOCK_TIMEOUT
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > SEED_LOCK_STALE:
                    lock.unlink(missing_ok=True)
                    continue
            except OSError:
                pass  # released meanwhile, or a stale lock that cannot be removed: wait like for a live one
        except PermissionError:  # Windows: the lock is being deleted right now
            pass
        else:
            os.close(fd)
            break
        if time.monotonic() >= deadline:
            raise SeedFileError(f"{path.name} is locked by another ULTRON process ({lock.name})")
        time.sleep(0.05)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def add_seeds(seeds: list[AtsBoardSeed], *, profile_slug: str) -> int:
    """Persist seeds to ``auto_seeds.yaml`` atomically; return how many are new.

    The read-modify-write runs under a lock file, so concurrent writers (two runs of different profiles)
    never drop each other's rows. Raises ``SeedFileError`` (file untouched, backup copied) if the existing
    file is unparseable or the lock cannot be taken. Rows that fail validation are preserved verbatim on
    rewrite.
    """
    path = auto_seeds_path()
    with _seed_file_lock(path):
        return _add_seeds_locked(path, seeds, profile_slug)


def _add_seeds_locked(path: Path, seeds: list[AtsBoardSeed], profile_slug: str) -> int:
    out_rows: list = []
    index: dict[tuple[str, str], AtsBoardSeed] = {}
    for raw in _read_raw_rows(path):
        parsed = _parse_rows([raw])
        if parsed:
            index[seed_identity(parsed[0].ats, parsed[0].board_identifier)] = parsed[0]
            out_rows.append(parsed[0])
        else:
            out_rows.append(raw)
    new = 0
    for seed in seeds:
        key = seed_identity(seed.ats, seed.board_identifier)
        current = index.get(key)
        if current is None:
            fresh = seed.model_copy(update={
                "seed_id": make_seed_id(seed.ats, seed.board_identifier),
                "profiles": list(dict.fromkeys([*seed.profiles, profile_slug])),
            })
            index[key] = fresh
            out_rows.append(fresh)
            new += 1
        elif profile_slug not in current.profiles:
            current.profiles.append(profile_slug)
    text = yaml.safe_dump(
        {"seeds": [r.model_dump(mode="json") if isinstance(r, AtsBoardSeed) else r for r in out_rows]},
        allow_unicode=True, sort_keys=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return new


@dataclass
class Proposal:
    company: str
    domain: str = ""
    ats: str = ""
    board: str = ""
    careers_url: str = ""
    source_url: str = ""


@dataclass
class VerifyResult:
    proposal: Proposal
    status: Literal["verified", "unresolved"]
    seed: AtsBoardSeed | None
    postings: int
    reason: str
    jobs: tuple = field(default=(), repr=False, compare=False)
    # For ``ownership_unknown`` / ``wrong_tenant``: the guessed board that answered ({ats, board, postings}).
    candidate: dict | None = None


# Trailing legal-form words that never belong in an ATS board slug ("Acme GmbH" -> "acme").
# Only legal forms: product words such as "ai" or "labs" can name a different company's board.
LEGAL_COMPANY_SUFFIXES = frozenset({
    "inc",
    "incorporated",
    "llc",
    "ltd",
    "limited",
    "corp",
    "corporation",
    "co",
    "plc",
    "gmbh",
    "ag",
    "sl",
    "slu",
    "sa",
    "sau",
    "sas",
    "srl",
    "bv",
    "oy",
    "ab",
    "pte",
    "pty",
})


# Letters NFKD does not decompose into an ASCII base letter.
_ASCII_FOLD = str.maketrans({"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "ł": "l", "đ": "d", "ð": "d", "þ": "th"})


def ascii_fold(text: str) -> str:
    """Lowercase ASCII transliteration: "Société Zürich Ñaña" -> "societe zurich nana" (NFKD, marks stripped)."""
    decomposed = unicodedata.normalize("NFKD", str(text or "").lower().translate(_ASCII_FOLD))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).encode("ascii", "ignore").decode("ascii")


def _core_company_words(company: str) -> list[str]:
    """Company name words with "&" spelled out, dotted initials joined and trailing legal forms dropped."""
    raw = ascii_fold(company).replace("&", " and ")
    raw = re.sub(r"\b(?:[a-z]\.)+[a-z]\b", lambda m: m.group(0).replace(".", ""), raw)
    words = re.findall(r"[a-z0-9]+", raw)
    while len(words) > 1 and (words[-1] in LEGAL_COMPANY_SUFFIXES or words[-1] == "and"):
        words.pop()
    return words


# Second-level labels of country domains (acme.co.uk, acme.com.au): the registrable name sits one label left.
COUNTRY_SLDS = frozenset({"co", "com", "org", "net"})
# Shared hosting and third-party platforms: a subdomain there (acme.github.io) names the platform's
# customer, and its registrable label (github) names the platform, never the company.
THIRD_PARTY_DOMAINS = frozenset({
    "github.com",
    "github.io",
    "gitlab.com",
    "gitlab.io",
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
    "wordpress.com",
    "wixsite.com",
    "squarespace.com",
    "webflow.io",
    "herokuapp.com",
    "netlify.app",
    "vercel.app",
    "pages.dev",
})
# Slugs that name no company: generic infrastructure or careers words (careers.acme.com, a company proposed as
# "Careers"). Probing them would verify whichever tenant owns that generic board (MARK II blocked labels).
BLOCKED_GENERIC_SLUGS = frozenset(GENERIC_BOARD_IDENTIFIERS) | frozenset({
    "www",
    "api",
    "boards",
    "board",
    "hire",
    "hiring",
    "join",
    "work",
    "talent",
    "remote",
    "recruiting",
    "recruitment",
})


def registrable_domain(domain: str) -> str:
    """Registrable domain (``acme.com`` for ``careers.acme.com``, ``acme.co.uk`` for ``jobs.acme.co.uk``).

    ``""`` for an empty, single-label or IP host, a bare country second-level domain (``co.uk``) and hosts on a
    shared-hosting or third-party platform (``acme.github.io``), whose registrable name is not the company's.
    """
    text = str(domain or "").strip()
    if not text:
        return ""
    try:
        host = (urlparse(text if "://" in text else f"//{text}").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""
    labels = [part for part in host.split(".") if part]
    if len(labels) < 2 or all(part.isdigit() for part in labels):
        return ""
    if labels[-2] in COUNTRY_SLDS and len(labels[-1]) == 2:
        if len(labels) < 3:
            return ""
        reg = labels[-3:]
    else:
        reg = labels[-2:]
    registrable = ".".join(reg)
    # A customer page on a platform (acme.github.io, docs.google.com) is not the company's site; the platform's
    # own apex (gitlab.com for GitLab) is.
    bare = host.removeprefix("www.")
    if any(bare.endswith("." + d) for d in THIRD_PARTY_DOMAINS):
        return ""
    return registrable


def domain_slug(domain: str) -> str:
    """The registrable domain's name label (``acme`` for ``es.acme.com``), ``""`` when it is unusable."""
    label = registrable_domain(domain).split(".")[0]
    return "" if label in BLOCKED_GENERIC_SLUGS else label


def _slug_candidate_passes(company: str, domain: str) -> tuple[list[str], list[str]]:
    """(literal name forms + domain label, at most two normalized forms not already in the first list).

    Generic slugs (``careers``, ``jobs``, ``remote``...) are never returned: they name no company.
    """
    words = re.findall(r"[a-z0-9]+", ascii_fold(company))
    literal =["".join(words), "-".join(words), domain_slug(domain)]
    primary = list(dict.fromkeys(s for s in literal if s and s not in BLOCKED_GENERIC_SLUGS))
    core = _core_company_words(company)
    normalized = [
        s for s in dict.fromkeys(("".join(core), "-".join(core)))
        if s and s not in primary and s not in BLOCKED_GENERIC_SLUGS
    ]
    return primary, normalized


def slug_candidates(company: str, domain: str) -> list[str]:
    """Board slugs to probe, in a fixed order: the literal name forms, the domain label, then at most
    two normalized forms (legal suffix stripped, "&" as "and", initials joined)."""
    primary, normalized = _slug_candidate_passes(company, domain)
    return primary + normalized


def company_key(company: str) -> str:
    """Normalized company identity: ASCII-folded core words joined ("Ñaña Labs S.L." -> "nanalabs")."""
    return "".join(_core_company_words(company))


def _vendor_company_names(jobs) -> set[str]:
    """Company names the vendor reports in its postings (Greenhouse/Recruitee ``company_name``,
    SmartRecruiters ``company.name``, ``companyName``), as company keys. Empty when it reports none."""
    names: set[str] = set()
    for job in list(jobs)[:20]:
        raw = getattr(job, "raw_payload", None)
        if not isinstance(raw, dict):
            continue
        company = raw.get("company")
        values = (
            raw.get("company_name"),
            raw.get("companyName"),
            company.get("name") if isinstance(company, dict) else company,
        )
        names.update(company_key(v) for v in values if isinstance(v, str) and v.strip())
    names.discard("")
    return names


# A name with one core word or a short key ("Acme", "Acme GmbH", "Nova") is shared by many companies: only a
# slug taken from the company's own domain ties such a board to it.
_SHORT_COMPANY_KEY = 6


def guessed_board_ownership(p: Proposal, board: str, jobs) -> Literal["owned", "wrong_tenant", "ownership_unknown"]:
    """Whether a board found by guessing ``board`` from the proposal belongs to the proposed company.

    ``owned`` when the slug is the proposal domain's registrable label, or when the name has two or more core
    words and the vendor reports the same company key. ``wrong_tenant`` when the vendor reports another company.
    ``ownership_unknown`` otherwise (a short or one-word name without a domain slug, or no vendor name).
    """
    slug = board.lower().replace("-", "")
    domain_label = domain_slug(p.domain).replace("-", "")
    if domain_label and slug == domain_label:
        return "owned"
    vendor_names = _vendor_company_names(jobs)
    if vendor_names and not vendor_names & _company_keys(p.company):
        return "wrong_tenant"
    if _is_short_name(p.company):
        return "ownership_unknown"
    return "owned" if vendor_names else "ownership_unknown"


def _is_short_name(company: str) -> bool:
    words = _core_company_words(company)
    return len(words) < 2 or len("".join(words)) < _SHORT_COMPANY_KEY


def _build_seed(p: Proposal, ats: str, board: str, postings: int) -> AtsBoardSeed:
    return AtsBoardSeed(
        seed_id=make_seed_id(ats, board),
        company=p.company,
        ats=ats,  # type: ignore[arg-type]
        board_identifier=board,
        source_type="agent_generated",
        source_label="Agent proposal",
        source_url=p.careers_url or p.source_url,
        verification={"postings": postings, "verified_at": datetime.now(UTC).isoformat()},
    )


FEED_PROPOSALS_FILE = "seed_proposals_from_feeds.jsonl"
SEARCH_PROPOSALS_FILE = "seed_proposals_from_search.jsonl"


def as_feed_company(seed: AtsBoardSeed) -> AtsBoardSeed:
    """Label a board verified from a feed company name (no agent proposed it)."""
    return seed.model_copy(update={"source_type": "feed_company", "source_label": "Feed company (verified)"})


# One capped hop from a company careers page (or an aggregator posting page) to the ATS boards it links
# (MARK II seed discovery expansion pass). Totals are per ``PageHops`` (one per ``seeds verify`` call or stage).
PAGE_HOP_TOTAL = 10
PAGE_HOP_PER_HOST = 2
PAGE_HOP_MAX_BOARDS = 3
_AGGREGATOR_REGISTRABLE = frozenset(d.removeprefix("www.") for d in AGGREGATOR_DOMAINS)


@dataclass
class PageHops:
    """Careers/aggregator page fetches left: at most ``total`` pages and ``per_host`` pages per host."""

    total: int = PAGE_HOP_TOTAL
    per_host: int = PAGE_HOP_PER_HOST
    used: dict[str, int] = field(default_factory=dict)

    def take(self, host: str) -> bool:
        if sum(self.used.values()) >= self.total or self.used.get(host, 0) >= self.per_host:
            return False
        self.used[host] = self.used.get(host, 0) + 1
        return True


class _DomainScopedPages:
    """``fetch_page_links`` client whose requests and redirect hops stay on the page's registrable domain or a
    supported ATS domain (a careers page that redirects to its hosted board)."""

    def __init__(self, client: HttpClient, domain: str) -> None:
        self._client = client
        self._domains = (domain, *SUPPORTED_ATS_DOMAINS)

    def get_text(self, url: str) -> tuple[int, str, str]:
        return self._client.get_text(url, allowed_domains=self._domains)


def _page_hop_target(p: Proposal, *, explicit: bool) -> tuple[str, str, str] | None:
    """``(url, registrable domain, kind)`` of the page to follow, kind ``company`` or ``aggregator``.

    With an explicit proposal only a ``careers_url`` that is not an ATS URL qualifies; otherwise the
    ``source_url`` (a feed posting page) does. A page is the company's own when it sits on the proposal's
    domain or, without a domain, when its domain label is the company key. Aggregator pages, and an agent's
    careers-looking ``careers_url`` on a domain that does not name the company, are judged as aggregators.
    """
    url = (p.careers_url if explicit else p.source_url).strip()
    if not url or urlparse(url).scheme not in {"http", "https"} or extract_seed_from_url(url, "") is not None:
        return None
    reg = registrable_domain(url)
    if not reg or is_direct_supported_ats_host(reg):
        return None
    if reg in _AGGREGATOR_REGISTRABLE:
        return url, reg, "aggregator"
    own = registrable_domain(p.domain)
    if own:
        return (url, reg, "company") if reg == own else None
    if domain_slug(reg).replace("-", "") in _company_keys(p.company):
        return url, reg, "company"
    if explicit and looks_like_company_jobs_url(url):
        return url, reg, "aggregator"
    return None


def _company_keys(company: str) -> set[str]:
    """The company key with and without legal forms ("acme", "acmegmbh" for "Acme GmbH")."""
    return {company_key(company), "".join(re.findall(r"[a-z0-9]+", ascii_fold(company)))} - {""}


def _aggregator_board_is_company(p: Proposal, board: str, jobs) -> bool:
    """A page that does not belong to the company links other companies' boards too: accept a board whose slug
    is the company key or whose vendor reports the company's name, never one whose vendor names another."""
    keys = _company_keys(p.company)
    vendor_names = _vendor_company_names(jobs)
    if vendor_names:
        return bool(vendor_names & keys)
    return board.lower().replace("-", "") in keys


def _linked_boards(links: list[str]) -> list[tuple[str, str]]:
    """Distinct ATS boards linked from a page: only links on a vendor host, never a slug built from a name."""
    boards: list[tuple[str, str]] = []
    for link in links:
        try:
            host = (urlparse(link).hostname or "").lower()
        except ValueError:
            continue
        if not host or not is_direct_supported_ats_host(host):
            continue
        hit = extract_seed_from_url(link, "")
        if hit is not None and (hit.ats, hit.board_identifier) not in boards:
            boards.append((hit.ats, hit.board_identifier))
    return boards


def verify_proposal(
    p: Proposal, *, client: HttpClient, timeout: int, verify_only: bool = False, page_hops: PageHops | None = None,
) -> VerifyResult:
    def unresolved(reason: str) -> VerifyResult:
        return VerifyResult(p, "unresolved", None, 0, reason)

    pairs: list[tuple[str, str]] = []
    has_explicit = bool((p.ats.strip() and p.board.strip()) or p.careers_url.strip())
    if p.ats and p.board:
        pairs.append((p.ats.strip().lower(), p.board.strip()))
    if p.careers_url:
        hit = extract_seed_from_url(p.careers_url, p.company)
        if hit is not None:
            pairs.append((hit.ats, hit.board_identifier))
    if not has_explicit:
        # Literal and domain slugs are probed on every vendor before any normalized guess, so "acme" on an
        # early vendor cannot preempt the company's literal "acmegmbh" board on a later one.
        primary, normalized = _slug_candidate_passes(p.company, p.domain)
        own_slug = domain_slug(p.domain)
        if own_slug and _is_short_name(p.company):
            # Only the domain slug can prove a short name's board, so the other guesses would only spend requests.
            primary, normalized = [own_slug], []
        for slugs in (primary, normalized):
            pairs.extend((vendor, slug) for vendor in PROBE_VENDORS for slug in slugs)
    # A short name without a domain slug can never be proven by guessing: report the first board that answers.
    stop_at_first_hit = not has_explicit and _is_short_name(p.company) and not domain_slug(p.domain)

    unsupported = invalid = host_budget = budget_spent = False
    doubtful: tuple[str, dict] | None = None  # (reason, candidate) of the best guessed board not proven owned
    skipped_vendors: set[str] = set()
    seen: set[tuple[str, str]] = set()
    answered: dict[tuple[str, str], list] = {}  # boards that answered, with their postings

    def probe(candidates: list[tuple[str, str]], mode: str) -> VerifyResult | None:
        """Try ``candidates``; ``mode``: explicit | guess | company_page | aggregator_page."""
        nonlocal unsupported, invalid, host_budget, budget_spent, doubtful
        for ats, board in candidates:
            key = seed_identity(ats, board)
            try:
                if key in seen:
                    if key not in answered:
                        continue
                    jobs = answered[key]  # a guessed board the page now links: judge it again, no new request
                elif ats in skipped_vendors:
                    continue
                else:
                    seen.add(key)
                    if verify_only:
                        jobs = fetch_vendor_jobs(ats, board, client=client, timeout=timeout, verify_only=True)
                    else:
                        jobs = fetch_vendor_jobs(ats, board, client=client, timeout=timeout)
                    answered[key] = jobs
            except RequestBudgetExceeded as exc:
                if getattr(exc, "scope", "global") == "host":
                    host_budget = True
                    skipped_vendors.add(ats)
                    continue
                budget_spent = True
                return None
            except AtsClientError as exc:
                unsupported = unsupported or str(exc) == "vendor_not_supported_yet"
                invalid = invalid or str(exc) == "invalid_board"
                continue
            except (HttpError, URLRejected, ValueError, KeyError, TypeError, AttributeError):
                continue
            if mode in {"guess", "aggregator_page"} and not jobs:
                # MARK II verification_probe rule: when probing guessed slugs, an empty
                # jobs array alone is never proof that the company owns that board.
                continue
            if mode == "guess":
                verdict = guessed_board_ownership(p, board, jobs)
                if verdict != "owned":
                    # Keep probing: a later vendor or slug may hold the company's own board.
                    if doubtful is None or (verdict == "ownership_unknown" and doubtful[0] == "wrong_tenant"):
                        doubtful = (verdict, {"ats": ats, "board": board, "postings": len(jobs)})
                    if stop_at_first_hit:
                        return None
                    continue
            if mode == "aggregator_page" and not _aggregator_board_is_company(p, board, jobs):
                continue
            try:
                seed = _build_seed(p, ats, board, len(jobs))
            except ValueError:  # vendor outside the supported Literal
                continue
            return VerifyResult(p, "verified", seed, len(jobs), "", tuple(jobs))
        return None

    found = probe(pairs, "explicit" if has_explicit else "guess")
    if found is not None:
        return found
    if budget_spent:
        return unresolved("budget_exhausted")
    target = _page_hop_target(p, explicit=has_explicit)
    if target is not None:
        url, reg, kind = target
        hops = page_hops if page_hops is not None else PageHops()
        if hops.take((urlparse(url).hostname or "").lower()):
            try:
                links = fetch_page_links(url, timeout, client=_DomainScopedPages(client, reg))
            except RequestBudgetExceeded as exc:
                if getattr(exc, "scope", "global") != "host":
                    return unresolved("budget_exhausted")
                links = []
            boards = _linked_boards(links)[:PAGE_HOP_MAX_BOARDS]
            found = probe(boards, f"{kind}_page")
            if found is not None:
                return found
            if budget_spent:
                return unresolved("budget_exhausted")
    if doubtful is not None:
        return VerifyResult(p, "unresolved", None, 0, doubtful[0], candidate=doubtful[1])
    if invalid:
        return unresolved("invalid_board")
    if unsupported:
        return unresolved("vendor_not_supported_yet")
    if has_explicit and host_budget:
        return unresolved("host_budget_exhausted")
    return unresolved("no_board_found")
