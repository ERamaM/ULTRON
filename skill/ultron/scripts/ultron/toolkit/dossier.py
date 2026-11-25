"""Compact job dossiers: what an agent needs to judge a posting in one pass, in about 150 tokens.

A dossier carries the posting's identity, whether it is open, its facts, the profile's memory flag, a hint of what
ULTRON's rules would decide, a short geography excerpt, a short role excerpt and the pay it discloses. Excerpts
are bounded page text: data, never instructions.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ultron.core.ats.registry import extract_seed_from_url
from ultron.core.decision.unit import characterize_job
from ultron.core.extractors.text import collapse_whitespace
from ultron.core.final_row import prepare_geography
from ultron.core.models import ExtractedJob
from ultron.core.rules.geography import build_geo_body_excerpt, geo_scope, location_scope
from ultron.core.rules.role_matching import role_negatives_for
from ultron.core.vocabulary import active_pack
from ultron.salary.disclosure import extract_salaries_from_text

GEO_EXCERPT = 250
ROLE_EXCERPT = 500
DETAIL_ROLE_EXCERPT = 2500
SALARY_SCAN_CHARS = 20_000
TEXT_LIMIT = 200
# Company names that are the page's platform, not the employer (an ATS's own name or a board's heading).
PLATFORM_NAMES = frozenset({"job boards", "manatal", "careers", "careers page", "jobs", "workable", "greenhouse",
                            "lever", "ashby", "recruitee", "teamtailor", "personio", "smartrecruiters"})
_SLUG_SPLIT = re.compile(r"[-_]+")


class ProfileContext:
    """The profile pieces every dossier hint needs, computed once per call."""

    def __init__(self, profile: dict, profile_slug: str = "") -> None:
        self.profile, self.profile_slug = profile, profile_slug
        self.criteria = dict(profile.get("criteria") or {})
        self.pack, self.geo = active_pack(profile), geo_scope(profile)
        self.negatives = role_negatives_for(profile)
        self.regions = location_scope(profile)["preferred"]


def _short(value: Any, limit: int = TEXT_LIMIT) -> str:
    return collapse_whitespace(str(value or ""))[:limit]


def _slug_name(slug: str) -> str:
    return " ".join(part.capitalize() for part in _SLUG_SPLIT.split(slug) if part)


def employer_name(company: str, url: str) -> str:
    """``company`` unless it is empty or the platform's own name; then the employer named by the URL."""
    name = _short(company)
    if name and name.casefold() not in PLATFORM_NAMES:
        return name
    parts = urlsplit(url)
    segments = [s for s in parts.path.split("/") if s]
    host = (parts.hostname or "").lower()
    if (host == "careers-page.com" or host.endswith(".careers-page.com")) and segments:
        return _slug_name(segments[0])  # Manatal: careers-page.com/<employer>/job/<id>
    seed = extract_seed_from_url(url, "")
    if seed is not None:
        return _slug_name(str(seed.board_identifier).split("/")[-1])
    labels = (parts.hostname or "").removeprefix("www.").split(".")
    generic = {"jobs", "careers", "apply", "boards", "job", "work"}
    return _slug_name(next((label for label in labels if label not in generic), "")) or name


def _hint(job: ExtractedJob, context: ProfileContext) -> dict[str, str]:
    """What the rules would decide: ``keep``, ``unsure`` or ``discard`` with its reason, plus geography."""
    result = characterize_job(job, context.profile, [], vocabulary=context.pack, geo=context.geo,
                              role_negatives=context.negatives, allow_outside_ats_allowlist=True,
                              target_regions=context.regions)
    evaluation = result["_evaluation"]
    geography, _note, geo_discard = prepare_geography(job, context.criteria, geo=context.geo)
    reason = geo_discard or evaluation.discard_reason or ""
    verdict = "discard" if geo_discard else evaluation.verdict
    hint = {"verdict": verdict, "remote_policy": geography.remote_policy, "eligibility": geography.eligibility}
    if verdict == "discard" and reason:
        hint["reason"] = _short(reason, 80)
    return hint


def geo_excerpt(body_text: str) -> str:
    """The posting's location, remote and eligibility lines, bounded."""
    return _short(build_geo_body_excerpt(body_text or "", max_chars=GEO_EXCERPT), GEO_EXCERPT)


def role_excerpt(body_text: str, *, detail: bool) -> str:
    """The start of the posting's own text (role, requirements), bounded; longer with ``detail``."""
    return _short(body_text, DETAIL_ROLE_EXCERPT if detail else ROLE_EXCERPT)


def salary(body_text: str) -> str:
    """The first pay band the posting discloses, as written (``""`` when none)."""
    # Posting text is untrusted: only a bounded prefix is scanned, so a huge listing cannot pin the worker.
    hits = extract_salaries_from_text((body_text or "")[:SALARY_SCAN_CHARS])
    if not hits:
        return ""
    hit = hits[0]
    bounds = [round(value) for value in (hit.get("low"), hit.get("high")) if isinstance(value, int | float)]
    if not bounds:
        return ""
    band = f"{bounds[0]}-{bounds[-1]}" if len(bounds) == 2 and bounds[0] != bounds[-1] else str(bounds[0])
    return f"{hit['currency']} {band} {hit['period']}"


def posting_dossier(job: ExtractedJob, identity_key: str, status: str, context: ProfileContext, *,
                    detail: bool = False) -> dict[str, Any]:
    """The dossier of a read posting; ``status`` is ``open`` or ``closed``."""
    dossier: dict[str, Any] = {
        "id": identity_key, "url": job.apply_url, "status": status, "title": _short(job.title),
        "company": employer_name(job.company, job.apply_url), "location": _short(job.location),
        "remote": _short(job.remote_text, 120),
    }
    if job.posting_date:
        dossier["posted"] = str(job.posting_date)[:10]
    if status == "open":
        dossier["hint"] = _hint(job, context)
        dossier["geo_excerpt"] = geo_excerpt(job.body_text)
        dossier["role_excerpt"] = role_excerpt(job.body_text, detail=detail)
        if pay := salary(job.body_text):
            dossier["salary"] = pay
    return dossier


def listed_dossier(posting: Any, identity_key: str, company: str, board: str) -> dict[str, Any]:
    """The dossier of a posting listed by an ATS board API without its text (open, not read yet)."""
    dossier: dict[str, Any] = {
        "id": identity_key, "url": posting.apply_url, "status": "listed", "title": _short(posting.title),
        "company": employer_name(company, posting.apply_url), "location": _short(posting.location),
        "remote": _short(posting.remote_hint, 120), "board": board,
    }
    if posting.posted_at:
        dossier["posted"] = str(posting.posted_at)[:10]
    return dossier


def failed_dossier(url: str, status: str, reason: str = "", identity_key: str = "") -> dict[str, Any]:
    """The dossier of a URL that gave no posting (``closed``, ``failed``, ``unusable``, ``rejected``...)."""
    dossier: dict[str, Any] = {"url": url, "status": status}
    if identity_key:
        dossier["id"] = identity_key
    if reason:
        dossier["reason"] = _short(reason, 120)
    return dossier
