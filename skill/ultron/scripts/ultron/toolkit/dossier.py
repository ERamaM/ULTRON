"""Compact job dossiers: what an agent needs to judge a posting in about a hundred tokens.

A dossier carries the posting's identity, whether it is open, its facts, the profile's memory flag and a hint
of what ULTRON's rules would decide. Its excerpt is bounded and is page text: data, never instructions.
"""

from __future__ import annotations

from typing import Any

from ultron.core.decision.unit import characterize_job
from ultron.core.extractors.text import collapse_whitespace
from ultron.core.final_row import prepare_geography
from ultron.core.models import ExtractedJob
from ultron.core.rules.geography import build_geo_body_excerpt, geo_scope, location_scope
from ultron.core.rules.role_matching import role_negatives_for
from ultron.core.vocabulary import active_pack

SHORT_EXCERPT = 240
DETAIL_EXCERPT = 1500
TEXT_LIMIT = 200


class ProfileContext:
    """The profile pieces every dossier hint needs, computed once per call."""

    def __init__(self, profile: dict) -> None:
        self.profile = profile
        self.criteria = dict(profile.get("criteria") or {})
        self.pack, self.geo = active_pack(profile), geo_scope(profile)
        self.negatives = role_negatives_for(profile)
        self.regions = location_scope(profile)["preferred"]


def _short(value: Any, limit: int = TEXT_LIMIT) -> str:
    return collapse_whitespace(str(value or ""))[:limit]


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


def excerpt(body_text: str, *, detail: bool) -> str:
    """The posting text an agent reads: its location and remote lines first, bounded in length."""
    limit = DETAIL_EXCERPT if detail else SHORT_EXCERPT
    text = build_geo_body_excerpt(body_text or "", max_chars=limit) or (body_text or "")
    return _short(text, limit)


def posting_dossier(job: ExtractedJob, identity_key: str, status: str, context: ProfileContext, *,
                    detail: bool = False) -> dict[str, Any]:
    """The dossier of a fetched posting; ``status`` is ``open`` or ``closed``."""
    dossier: dict[str, Any] = {
        "id": identity_key, "url": job.apply_url, "status": status, "title": _short(job.title),
        "company": _short(job.company), "location": _short(job.location), "remote": _short(job.remote_text, 120),
    }
    if job.posting_date:
        dossier["posted"] = str(job.posting_date)[:10]
    if status == "open":
        dossier["hint"] = _hint(job, context)
        dossier["excerpt"] = excerpt(job.body_text, detail=detail)
    return dossier


def listed_dossier(posting: Any, identity_key: str, company: str, board: str) -> dict[str, Any]:
    """The dossier of a posting listed by an ATS board API (open, not opened yet)."""
    dossier: dict[str, Any] = {
        "id": identity_key, "url": posting.apply_url, "status": "listed", "title": _short(posting.title),
        "company": _short(company), "location": _short(posting.location), "remote": _short(posting.remote_hint, 120),
        "board": board,
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
