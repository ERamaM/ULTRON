"""Collect configured profile terms only from their permitted job fields."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ultron.core.models import ExtractedJob
from ultron.core.rules.hard_filters import build_geo_body_excerpt
from ultron.core.rules.term_matching import find_terms


@dataclass(frozen=True, slots=True)
class ProfileTermHits:
    exclusion_terms: tuple[str, ...] = ()
    seniority_excluded: tuple[str, ...] = ()
    remote_negative: tuple[str, ...] = ()
    location_excluded: tuple[str, ...] = ()
    remote_positive: tuple[str, ...] = ()
    location_eligibility: tuple[str, ...] = ()

    @property
    def penalties(self) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                self.exclusion_terms + self.seniority_excluded + self.remote_negative + self.location_excluded
            )
        )


def _terms(criteria: Mapping[str, Any], section: str, field: str) -> tuple[str, ...]:
    source = criteria.get(section) if section else criteria
    value = source.get(field) if isinstance(source, Mapping) else None
    return (
        tuple(term.strip() for term in value if isinstance(term, str) and term.strip())
        if isinstance(value, (list, tuple))
        else ()
    )


def exclusion_lists_configured(criteria: Mapping[str, Any]) -> bool:
    return any(
        _terms(criteria, section, field)
        for section, field in (
            ("", "exclusion_terms"),
            ("seniority", "excluded_terms"),
            ("remote_policy", "negative_terms"),
            ("location_scope", "excluded_terms"),
        )
    )


def collect_profile_term_hits(job: ExtractedJob, criteria: Mapping[str, Any]) -> ProfileTermHits:
    remote = "\n".join((job.remote_text or "", job.location or ""))
    location = "\n".join((job.location or "", job.remote_text or "", build_geo_body_excerpt(job.body_text)))
    return ProfileTermHits(
        exclusion_terms=tuple(
            find_terms(
                "\n".join((job.title or "", job.location or "", job.remote_text or "")),
                _terms(criteria, "", "exclusion_terms"),
            )
        ),
        seniority_excluded=tuple(find_terms(job.title, _terms(criteria, "seniority", "excluded_terms"))),
        remote_negative=tuple(find_terms(remote, _terms(criteria, "remote_policy", "negative_terms"))),
        location_excluded=tuple(find_terms(location, _terms(criteria, "location_scope", "excluded_terms"))),
        remote_positive=tuple(find_terms(remote, _terms(criteria, "remote_policy", "positive_terms"))),
        location_eligibility=tuple(find_terms(location, _terms(criteria, "location_scope", "eligibility_terms"))),
    )
