"""Company and domain names: folding, registrable domains and board-slug candidates."""

from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlparse

from ultron.core.ats.registry import GENERIC_BOARD_IDENTIFIERS
from ultron.core.feeds.urls import COUNTRY_SLDS

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


def core_company_words(company: str) -> list[str]:
    """Company name words with "&" spelled out, dotted initials joined and trailing legal forms dropped."""
    raw = ascii_fold(company).replace("&", " and ")
    raw = re.sub(r"\b(?:[a-z]\.)+[a-z]\b", lambda m: m.group(0).replace(".", ""), raw)
    words = re.findall(r"[a-z0-9]+", raw)
    while len(words) > 1 and (words[-1] in LEGAL_COMPANY_SUFFIXES or words[-1] == "and"):
        words.pop()
    return words


# Second-level labels of country domains (acme.co.uk, acme.com.au): the registrable name sits one label left.
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


def slug_candidate_passes(company: str, domain: str) -> tuple[list[str], list[str]]:
    """(literal name forms + domain label, at most two normalized forms not already in the first list).

    Generic slugs (``careers``, ``jobs``, ``remote``...) are never returned: they name no company.
    """
    words = re.findall(r"[a-z0-9]+", ascii_fold(company))
    literal =["".join(words), "-".join(words), domain_slug(domain)]
    primary = list(dict.fromkeys(s for s in literal if s and s not in BLOCKED_GENERIC_SLUGS))
    core = core_company_words(company)
    normalized = [
        s for s in dict.fromkeys(("".join(core), "-".join(core)))
        if s and s not in primary and s not in BLOCKED_GENERIC_SLUGS
    ]
    return primary, normalized


def slug_candidates(company: str, domain: str) -> list[str]:
    """Board slugs to probe, in a fixed order: the literal name forms, the domain label, then at most
    two normalized forms (legal suffix stripped, "&" as "and", initials joined)."""
    primary, normalized = slug_candidate_passes(company, domain)
    return primary + normalized


def company_key(company: str) -> str:
    """Normalized company identity: ASCII-folded core words joined ("Ñaña Labs S.L." -> "nanalabs")."""
    return "".join(core_company_words(company))


