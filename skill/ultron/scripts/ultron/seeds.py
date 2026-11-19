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
import unicodedata
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import yaml

from ultron import home
from ultron.core.ats.clients import AtsClientError, fetch_vendor_jobs
from ultron.core.ats.identity import seed_identity
from ultron.core.ats.registry import GENERIC_BOARD_IDENTIFIERS, extract_seed_from_url
from ultron.core.models import AtsBoardSeed
from ultron.core.sectors import seed_sector_values
from ultron.net import HttpClient, HttpError, RequestBudgetExceeded, URLRejected

logger = logging.getLogger(__name__)

# Vendors probed by slug, in order. The rest (workday, icims, taleo, jobvite, bamboohr, jazzhr,
# factorial, comeet) need a host or a vendor-specific locator, so they are only reached via an
# explicit ats/board pair or a careers_url.
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


def add_seeds(seeds: list[AtsBoardSeed], *, profile_slug: str) -> int:
    """Persist seeds to ``auto_seeds.yaml`` atomically; return how many are new.

    Raises ``SeedFileError`` (file untouched, backup copied) if the existing file is
    unparseable. Rows that fail validation are preserved verbatim on rewrite.
    """
    path = auto_seeds_path()
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
    if any(host == d or host.endswith("." + d) for d in THIRD_PARTY_DOMAINS):
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


def verify_proposal(
    p: Proposal, *, client: HttpClient, timeout: int, verify_only: bool = False
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
        for slugs in (primary, normalized):
            pairs.extend((vendor, slug) for vendor in PROBE_VENDORS for slug in slugs)

    unsupported = invalid = host_budget = False
    skipped_vendors: set[str] = set()
    seen: set[tuple[str, str]] = set()
    for ats, board in pairs:
        key = seed_identity(ats, board)
        if key in seen:
            continue
        seen.add(key)
        if ats in skipped_vendors:
            continue
        try:
            if verify_only:
                jobs = fetch_vendor_jobs(ats, board, client=client, timeout=timeout, verify_only=True)
            else:
                jobs = fetch_vendor_jobs(ats, board, client=client, timeout=timeout)
        except RequestBudgetExceeded as exc:
            if getattr(exc, "scope", "global") == "host":
                host_budget = True
                skipped_vendors.add(ats)
                continue
            return unresolved("budget_exhausted")
        except AtsClientError as exc:
            unsupported = unsupported or str(exc) == "vendor_not_supported_yet"
            invalid = invalid or str(exc) == "invalid_board"
            continue
        except (HttpError, URLRejected, ValueError, KeyError, TypeError, AttributeError):
            continue
        if not has_explicit and not jobs:
            # MARK II verification_probe rule: when probing guessed slugs, an empty
            # jobs array alone is never proof that the company owns that board.
            continue
        try:
            seed = _build_seed(p, ats, board, len(jobs))
        except ValueError:  # vendor outside the supported Literal
            continue
        return VerifyResult(p, "verified", seed, len(jobs), "", tuple(jobs))
    if invalid:
        return unresolved("invalid_board")
    if unsupported:
        return unresolved("vendor_not_supported_yet")
    if has_explicit and host_budget:
        return unresolved("host_budget_exhausted")
    return unresolved("no_board_found")
