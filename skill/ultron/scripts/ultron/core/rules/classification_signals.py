from __future__ import annotations

import re
from collections.abc import Iterable

from ultron.core.models import ClassificationSignals, ExtractedJob, ExtractionMeta, GeoScope
from ultron.core.rules.geography import build_geo_body_excerpt, scoped_posting_eligibility
from ultron.core.rules.hard_filters import classify_eligibility, classify_remote_policy
from ultron.core.rules.hard_filters_data import DISALLOWED_REMOTE_TERMS, STRONG_REMOTE_POSITIVE_TERMS
from ultron.core.rules.role_matching import (
    DEFAULT_ROLE_NEGATIVES,
    RoleNegatives,
    role_ambiguity_detected,
    score_role_family,
)
from ultron.core.rules.term_matching import find_terms, normalize_text
from ultron.core.vocabulary import EMPTY_PACK, VocabularyPack

EXPERIENCE_SIGNAL_PATTERNS = (
    # Negative lookbehind so the "5 years" inside ranges like "3-5 years"
    # (or "3–5 years") does not count as a 5+ years senior signal.
    r"(?<![\d\-–—])\b[3-4]\+\s+(?:years?|a[ñn]os)\b",
    r"(?<![\d\-–—])\b[5-9]\+?\s+years?\b",
    r"(?<![\d\-–—])\b[5-9]\+?\s+a[ñn]os\b",
    r"\b\d{2}\+?\s+years?\b",
    r"\b\d{2}\+?\s+a[ñn]os\b",
    r"\bmento(?:r|ring)\b",
    r"\bown(?:ership)?\b",
    r"\barchitect(?:ure|ing)?\b",
    r"\bsystem design\b",
)


def build_classification_signals(
    job: ExtractedJob,
    *,
    extraction_meta: ExtractionMeta | None = None,
    role_focus_terms: Iterable[str] | None = None,
    role_signal_terms: Iterable[str] | None = None,
    data_scientist_signal_terms: Iterable[str] | None = None,
    role_ambiguity_terms: Iterable[str] | None = None,
    seniority_preferred_terms: Iterable[str] | None = None,
    vocabulary: VocabularyPack = EMPTY_PACK,
    geo: GeoScope = "eu",
    eligibility_terms: Iterable[str] = (),
    role_negatives: RoleNegatives = DEFAULT_ROLE_NEGATIVES,
) -> ClassificationSignals:
    """Pre-evaluation signals. ``geo``/``eligibility_terms`` pick the geography rules (EU only for ``"eu"``)."""
    role_score, role_signals = score_role_family(
        job.title,
        job.body_text,
        allowed_terms=role_focus_terms,
        strong_signal_terms=role_signal_terms,
        data_scientist_signal_terms=data_scientist_signal_terms,
        vocabulary=vocabulary,
        negatives=role_negatives,
    )
    geo_excerpt = build_geo_body_excerpt(job.body_text)
    geo_text = f"{job.location}\n{job.remote_text}\n{geo_excerpt}"
    role_text = f"{job.title}\n{job.body_text}"
    low_geo_text = geo_text.lower()
    low_role_text = role_text.lower()

    remote_conflict = None
    if job.remote_text:
        remote_low = normalize_text(job.remote_text)
        has_disallowed = any(term in remote_low for term in DISALLOWED_REMOTE_TERMS)
        has_strong_positive = any(term in remote_low for term in STRONG_REMOTE_POSITIVE_TERMS)
        remote_conflict = has_disallowed and not has_strong_positive

    exp_signal = any(re.search(pattern, low_role_text, flags=re.IGNORECASE) for pattern in EXPERIENCE_SIGNAL_PATTERNS)
    seniority_ambiguous = exp_signal and not any(token in (job.title or "").lower() for token in ("senior", "sr."))
    if seniority_preferred_terms:
        seniority_ambiguous = exp_signal and not bool(find_terms(job.title, seniority_preferred_terms))

    role_ambiguous = role_ambiguity_detected(role_score, low_role_text, role_ambiguity_terms, vocabulary=vocabulary)

    sources: list[str] = []
    if extraction_meta is not None:
        for source in (
            extraction_meta.title_source,
            extraction_meta.company_source,
            extraction_meta.location_source,
            extraction_meta.remote_source,
        ):
            if source != "unknown" and source not in sources:
                sources.append(source)
    if not sources:
        sources = ["unknown"]

    return ClassificationSignals(
        role_score=role_score,
        role_signals=role_signals[:6],
        role_ambiguous=role_ambiguous,
        remote_scope_hint=classify_remote_policy(
            low_geo_text, geo, remote_text=job.remote_text, body_text=job.body_text
        ),
        remote_conflict=remote_conflict,
        eligibility_hint=(
            classify_eligibility(low_geo_text)
            if geo == "eu"
            else scoped_posting_eligibility(job.location, job.remote_text, job.body_text, eligibility_terms)
        ),
        seniority_ambiguous=seniority_ambiguous,
        signal_sources=sources[:4],
    )
