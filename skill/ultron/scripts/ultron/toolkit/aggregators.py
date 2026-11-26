"""Company clues and targeted ATS queries for unlinked aggregator listings."""

from itertools import pairwise
from urllib.parse import unquote, urlsplit

from ultron.core.ats.registry import extract_seed_from_url
from ultron.core.rules.term_matching import normalize_text
from ultron.seeds.naming import company_key, slug_candidates
from ultron.stages.scout_resolve import is_job_link
from ultron.toolkit.dossier import employer_name

_PATH_MARKERS = frozenset({"company", "companies", "employers", "employer"})
_ATS_FILTER = "site:greenhouse.io OR site:ashbyhq.com OR site:lever.co"
_TITLE_NOISE = frozenset({"senior", "sr", "junior", "jr", "staff", "principal", "lead", "head", "the", "of",
                         "a", "an", "and", "hiring", "job", "remote", "hybrid", "onsite"})
_OCCUPATIONS = frozenset({"engineer", "developer", "analyst", "manager", "scientist", "consultant", "specialist"})
_ABBREVIATIONS = {"ai": ("artificial", "intelligence"), "ml": ("machine", "learning")}


def aggregator_company(url: str) -> str:
    """Return the employer when an aggregator path names it explicitly."""
    parts = urlsplit(url)
    segments = parts.path.strip("/").split("/")
    for marker, raw in pairwise(segments):
        if marker.casefold() not in _PATH_MARKERS:
            continue
        slug = unquote(raw).casefold()
        if 1 <= len(slug) <= 60 and slug.isascii() and slug.replace("-", "").isalnum():
            return employer_name("", f"https://{slug}.example")
    return ""


def title_company(title: str) -> str:
    """Extract a company clue from common employer-marked search result titles."""
    title = title[:250]
    for separator in (" at ", " @ "):
        _, found, name = title.rpartition(separator)
        if found and (company := name.split(" | ", 1)[0].strip()):
            return company[:100]
    name, found, role = title.partition(" - ")
    return name.strip()[:100] if found and role.strip() else ""


def _words(text: str) -> list[str]:
    return "".join(char if char.isalnum() else " " for char in normalize_text(text)).split()


def lookup_query(company: str, title: str) -> str:
    """Build a tightly scoped query from bounded employer and role tokens."""
    name = " ".join(_words(company[:100])[:8])
    role = " ".join(_words(title[:150])[:8])
    return f'"{name}" {role} ({_ATS_FILTER})' if name and role else ""


def _role_terms(title: str, company: str) -> set[str]:
    title = title[:250].split(" | ", 1)[0]
    for separator in (" at ", " @ "):
        role, found, _company = title.rpartition(separator)
        if found:
            title = role
            break
    left, found, right = title.partition(" - ")
    if found and company_key(left) == company_key(company):
        title = right
    elif found and company_key(right) == company_key(company):
        title = left
    return {term for word in _words(title) if word not in _TITLE_NOISE for term in _ABBREVIATIONS.get(word, (word,))}


def matches_lookup(url: str, result_title: str, company: str, title: str) -> bool:
    """Accept a targeted hit only when its ATS tenant and title match the listing."""
    if not is_job_link(url) or (seed := extract_seed_from_url(url, "")) is None:
        return False
    if seed.ats not in {"ashby", "greenhouse", "lever"}:
        return False
    keys = {company_key(name) for name in (company, *slug_candidates(company, ""))}
    board = company_key(str(seed.board_identifier))
    role = _role_terms(title, company)
    return bool(board and board in keys and role - _OCCUPATIONS and role <= _role_terms(result_title, company))
