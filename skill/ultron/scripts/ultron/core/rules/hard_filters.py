from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Iterator

from ultron.core.models import ClassificationSignals, Eligibility, ExtractedJob, ExtractionMeta, GeoScope, RemotePolicy
from ultron.core.rules import hard_filters_data as rules_data
from ultron.core.rules import hard_filters_signals as signals

# European geography rules live in ``geo_eu``; they are re-exported here because MARK III
# callers and tests address them through this module.
from ultron.core.rules.geo_eu import (  # noqa: F401 - re-exported names
    _fold_accents,
    _has_disallowed_region_terms,
    _has_est_timezone_signal,
    _has_eu_country_in_location_context,
    _has_eu_eligibility_signal,
    _has_non_eu_location_in_context,
    _has_pattern_match,
    _has_positive_spain_signal,
    _has_security_clearance_requirement,
    _has_term_in_location_context,
    _has_us_location_signal,
    _has_us_only_signal,
    _has_us_state_in_location_context,
    _iter_term_windows,
    _window_has_us_disqualifier,
    classify_eu_eligibility,
    classify_eu_remote_policy,
    has_region_red_flags,
)
from ultron.core.rules.role_matching import (
    DEFAULT_ROLE_NEGATIVES,
    RoleNegatives,
    role_ambiguity_detected,
    score_role_family,
)
from ultron.core.rules.term_matching import find_terms, normalize_text
from ultron.core.rules.text_patterns import (
    contains_any_substring,
    contains_any_word_boundary,
    term_boundary_pattern,
    word_boundary_pattern,
)
from ultron.core.vocabulary import EMPTY_PACK, VocabularyPack

logger = logging.getLogger(__name__)


def build_geo_body_excerpt(body_text: str, *, max_chars: int = 1200) -> str:
    text = (body_text or "").strip()
    if not text:
        return ""
    normalized_max = max(250, int(max_chars))
    selected_lines: list[str] = []
    seen: set[str] = set()
    total = 0
    for raw_line in re.split(r"[\r\n]+", text or ""):
        line = " ".join(raw_line.split()).strip()
        if not line:
            continue
        low = line.lower()
        if not contains_any_substring(low, rules_data.GEO_BODY_CONTEXT_TERMS):
            continue
        key = low
        if key in seen:
            continue
        seen.add(key)
        chunk = line[:400]
        extra = len(chunk) + (1 if selected_lines else 0)
        if selected_lines and total + extra > normalized_max:
            break
        selected_lines.append(chunk)
        total += extra
        if total >= normalized_max:
            break
    return "\n".join(selected_lines)


def _has_contextual_hybrid_conflict(body_text: str) -> bool:
    low = body_text.lower()
    for match in word_boundary_pattern("hybrid").finditer(low):
        start = max(0, match.start() - 70)
        end = min(len(low), match.end() + 70)
        window = low[start:end]
        if any(token in window for token in rules_data.HYBRID_NEGATION_TERMS):
            continue
        if any(token in window for token in rules_data.HYBRID_TECH_CONTEXT_TERMS) and not any(
            token in window for token in rules_data.HYBRID_WORK_CONTEXT_TERMS
        ):
            continue
        if any(token in window for token in rules_data.HYBRID_WORK_CONTEXT_TERMS):
            return True
    return False


def _has_contextual_onsite_conflict(body_text: str) -> bool:
    low = body_text.lower()
    negation_terms = rules_data.ONSITE_NEGATION_TERMS | rules_data.REMOTE_CONFLICT_NEGATION_TERMS
    for pattern in rules_data.ONSITE_BODY_CONFLICT_PATTERNS:
        for match in re.finditer(pattern, low, re.IGNORECASE):
            context_start = max(0, match.start() - 45)
            context_end = min(len(low), match.end() + 25)
            context = low[context_start:context_end]
            if any(negation in context for negation in negation_terms):
                continue
            return True

    for term in rules_data.ONSITE_BODY_CONFLICT_TERMS:
        pattern = re.compile(re.escape(term), re.IGNORECASE)
        for match in pattern.finditer(low):
            context_start = max(0, match.start() - 45)
            context_end = min(len(low), match.end() + 25)
            context = low[context_start:context_end]
            if any(negation in context for negation in negation_terms):
                continue
            return True

    for match in _iter_onsite_term_matches(low):
        context_start = max(0, match.start() - 60)
        context_end = min(len(low), match.end() + 25)
        context = low[context_start:context_end]
        if any(negation in context for negation in negation_terms):
            continue
        if any(perk in context for perk in rules_data.ONSITE_BENEFIT_CONTEXT_TERMS):
            continue
        return True
    return False


_ONSITE_TERM_PRODUCT_SUFFIX_RE = re.compile(rules_data.ONSITE_TERM_PRODUCT_SUFFIX_PATTERN, re.IGNORECASE)


def _iter_onsite_term_matches(text: str) -> Iterator[re.Match[str]]:
    """Yield whole-term ONSITE_TERMS matches ("Berlin office" and "in Office 365" do not count)."""
    for term in rules_data.ONSITE_TERMS:
        for match in term_boundary_pattern(term).finditer(text):
            if not _ONSITE_TERM_PRODUCT_SUFFIX_RE.match(text, match.end()):
                yield match


def _has_closed_status_with_context(text: str) -> bool:
    low = normalize_text(text)
    for term in ("closed", "expired"):
        for match in word_boundary_pattern(term).finditer(low):
            if term == "closed" and signals.closed_match_is_technical_or_negated(low, match):
                continue
            start = max(0, match.start() - 45)
            end = min(len(low), match.end() + 45)
            window = low[start:end]
            if contains_any_word_boundary(window, rules_data.CLOSED_STATUS_EXEMPT_TERMS):
                continue
            if contains_any_word_boundary(window, rules_data.CLOSED_STATUS_POSITION_CONTEXT_TERMS):
                return True
    return False


def _is_allowed_domain(domain: str, allowed_domains: Iterable[str]) -> bool:
    normalized = (domain or "").lower().strip()
    if not normalized:
        return False
    return any(normalized == d.lower() or normalized.endswith(f".{d.lower()}") for d in allowed_domains if d)


def is_full_remote_only(remote_text: str, body_text: str = "") -> bool:
    remote_low = normalize_text(remote_text)
    body_low = normalize_text(body_text)
    full_text = f"{remote_low}\n{body_low}"

    if not signals.has_remote_work_hint(full_text):
        return False
    # If the remote_text field explicitly says hybrid/onsite, reject.
    if contains_any_substring(remote_low, rules_data.DISALLOWED_REMOTE_TERMS):
        # But allow if strong remote-positive terms are also present in remote_text.
        return bool(contains_any_substring(remote_low, rules_data.STRONG_REMOTE_POSITIVE_TERMS))
    # Strong remote signals in remote_text override body-text conflicts.
    if contains_any_substring(remote_low, rules_data.STRONG_REMOTE_POSITIVE_TERMS):
        return True
    # Only check body for conflicts — body boilerplate can trigger false positives,
    # so we only reject if there's a strong on-site/hybrid signal in body.
    if _has_contextual_onsite_conflict(body_low):
        return False
    return not _has_contextual_hybrid_conflict(body_low)


def is_open_status_ok(text: str, apply_available: bool, is_open_hint: bool) -> bool:
    """Reject only explicit closed-status phrases.

    ``apply_available`` and ``is_open_hint`` are intentionally not trusted:
    ATS buttons and heuristic hints are often missing or stale, while an
    explicit closed phrase in the body is actionable evidence.
    """

    low = normalize_text(text)
    if contains_any_substring(low, rules_data.CLOSED_STATUS_PHRASES):
        return False
    if _has_closed_status_with_context(low):
        return False
    # A missing apply button is weak evidence: many ATS templates hide it from
    # scraped HTML. Only explicit closed phrases reject the posting.
    return True


def classify_remote_policy(
    text: str, geo: GeoScope = "eu", *, remote_text: str = "", body_text: str = ""
) -> RemotePolicy | None:
    """Classify explicit remote geography without inventing missing evidence.

    European policies come from ``geo_eu`` (on ``text``) for every scope: they describe
    the posting. Outside a European scope, a posting with no European policy is
    ``"remote_other"`` when it is full remote, decided on the split ``remote_text`` /
    ``body_text`` fields exactly like the evaluator (``is_full_remote_only``), so body
    phrases such as "hybrid cloud" only count through the contextual body checks.
    ``None`` means the text gave no reliable policy signal.
    """
    policy = classify_eu_remote_policy(text)
    if geo == "eu" or policy is not None:
        return policy
    return "remote_other" if is_full_remote_only(remote_text, body_text) else None


def classify_eligibility(
    text: str, geo: GeoScope = "eu", eligibility_terms: Iterable[str] = ()
) -> Eligibility | None:
    """Classify eligibility from textual evidence for the profile's geographic scope.

    European scope: the EU rules of ``geo_eu``. Other scopes: ``"high"`` only when the
    text names one of the profile's ``location_scope.eligibility_terms``, else ``None``
    (the agent decides). The result is a confidence label, not an acceptance decision.
    """
    if geo == "eu":
        return classify_eu_eligibility(text)
    return "high" if find_terms(text, tuple(eligibility_terms)) else None


def location_excluded(text: str, excluded_terms: Iterable[str]) -> bool:
    """True when the text names one of the profile's ``location_scope.excluded_terms``."""
    return bool(find_terms(text, tuple(excluded_terms)))


def apply_hard_filters(
    job: ExtractedJob,
    allowed_domains: list[str],
    *,
    allow_outside_ats_allowlist: bool = False,
    recall_mode_active: bool = False,
    soft_reasons: list[str] | None = None,
    remote_required: bool = True,
    target_regions: Iterable[str] | None = None,
    geo: GeoScope = "eu",
) -> tuple[bool, str | None]:
    """Apply authoritative domain, remote, geography, and status gates.

    With ``recall_mode_active`` clear remote/geography negatives are appended
    to ``soft_reasons`` instead of being rejected. ``target_regions`` controls
    whether the European geography guard runs; outside a European ``geo`` scope it
    never runs. The return value is ``(accepted, discard_reason)``.
    """
    if not allow_outside_ats_allowlist and not _is_allowed_domain(job.domain, allowed_domains):
        return False, "outside_ats_allowlist"

    # Only reject remote policy when there IS remote-related evidence.
    # Jobs with no remote hints at all (neither positive nor negative) should
    # pass through to the LLM/guardrail instead of being treated as non-remote.
    full_text_low = normalize_text(f"{job.remote_text}\n{job.body_text}")
    has_positive_remote_hint = contains_any_substring(full_text_low, rules_data.REMOTE_HINT_TERMS)
    has_negative_remote_hint = contains_any_substring(
        full_text_low, rules_data.DISALLOWED_REMOTE_TERMS
    ) or next(_iter_onsite_term_matches(full_text_low), None) is not None
    if (
        remote_required
        and (has_positive_remote_hint or has_negative_remote_hint)
        and not is_full_remote_only(job.remote_text, job.body_text)
    ):
        if recall_mode_active:
            if soft_reasons is not None:
                soft_reasons.append("hybrid_or_not_explicit_remote")
        else:
            return False, "hybrid_or_not_explicit_remote"

    # Geography composes the classifiers in this module; defer import to avoid a cycle.
    from ultron.core.rules.geography import assess_geography

    geography = (
        assess_geography(
            location=job.location,
            remote_text=job.remote_text,
            body_text=job.body_text,
            target_regions=target_regions or (),
        )
        if geo == "eu"
        else None
    )
    if geography is not None and geography.conflict:
        if recall_mode_active:
            if soft_reasons is not None:
                soft_reasons.append("non_eu_remote")
        else:
            return False, "non_eu_remote"

    if not is_open_status_ok(job.body_text, job.apply_available, job.is_open_hint):
        return False, "expired_or_closed"

    return True, None


def pre_filter_candidate(
    job: ExtractedJob,
    allowed_domains: list[str],
    role_focus_terms: Iterable[str] | None = None,
    *,
    extraction_meta: ExtractionMeta | None = None,
    classification_signals: ClassificationSignals | None = None,
    allow_outside_ats_allowlist: bool = False,
    recall_mode_active: bool = False,
    soft_reasons: list[str] | None = None,
    remote_required: bool = True,
    target_regions: Iterable[str] | None = None,
    role_signal_terms: Iterable[str] | None = None,
    data_scientist_signal_terms: Iterable[str] | None = None,
    vocabulary: VocabularyPack = EMPTY_PACK,
    geo: GeoScope = "eu",
    role_negatives: RoleNegatives = DEFAULT_ROLE_NEGATIVES,
) -> tuple[bool, str | None]:
    """Reject only clear negatives before expensive fetch/evaluation.

    In recall mode, ambiguous remote/role/eligibility signals are recorded in
    ``soft_reasons`` and allowed through for downstream classification. The European
    geography guard only runs for a European ``geo`` scope.
    """
    if not allow_outside_ats_allowlist and not _is_allowed_domain(job.domain, allowed_domains):
        return False, "outside_ats_allowlist"

    if not is_open_status_ok(job.body_text, job.apply_available, job.is_open_hint):
        return False, "expired_or_closed"

    if classification_signals is None:
        role_score, _role_hits = score_role_family(
            job.title,
            job.body_text,
            allowed_terms=role_focus_terms,
            strong_signal_terms=role_signal_terms,
            data_scientist_signal_terms=data_scientist_signal_terms,
            vocabulary=vocabulary,
            negatives=role_negatives,
        )
        role_ambiguous = role_ambiguity_detected(role_score, f"{job.title}\n{job.body_text}", vocabulary=vocabulary)
        remote_conflict = None
        eligibility_hint = None
    else:
        role_score = int(classification_signals.role_score)
        role_ambiguous = bool(classification_signals.role_ambiguous)
        remote_conflict = classification_signals.remote_conflict
        eligibility_hint = classification_signals.eligibility_hint

    if role_score < rules_data.ROLE_FAMILY_CLEAR_REJECT_SCORE and not role_ambiguous:
        logger.debug(
            "prefilter reject reason=non_matching_role_family role_score=%s title=%s",
            role_score,
            (job.title or "")[:100],
        )
        return False, "non_matching_role_family"

    # Only reject remote policy when the remote_text field explicitly says hybrid/onsite.
    # Don't scan body_text — ATS listings often have boilerplate that triggers false positives.
    remote_low = normalize_text(job.remote_text)
    explicit_remote_conflict = contains_any_substring(remote_low, rules_data.DISALLOWED_REMOTE_TERMS)
    # Strong positive overrides (e.g. "remote-first", "fully remote") cancel out conflict terms.
    if explicit_remote_conflict and contains_any_substring(remote_low, rules_data.STRONG_REMOTE_POSITIVE_TERMS):
        explicit_remote_conflict = False
    if remote_required and (explicit_remote_conflict or remote_conflict is True):
        if recall_mode_active:
            if soft_reasons is not None:
                soft_reasons.append("hybrid_or_not_explicit_remote")
            logger.debug(
                "prefilter soften reason=hybrid_or_not_explicit_remote remote_source=%s title=%s",
                getattr(extraction_meta, "remote_source", "unknown"),
                (job.title or "")[:100],
            )
        else:
            logger.debug(
                "prefilter reject reason=hybrid_or_not_explicit_remote remote_source=%s title=%s",
                getattr(extraction_meta, "remote_source", "unknown"),
                (job.title or "")[:100],
            )
            return False, "hybrid_or_not_explicit_remote"
    # Geography composes the classifiers in this module; defer import to avoid a cycle.
    from ultron.core.rules.geography import assess_geography

    geography = (
        assess_geography(
            location=job.location,
            remote_text=job.remote_text,
            body_text=job.body_text,
            target_regions=target_regions or (),
            eligibility_hint=eligibility_hint,
        )
        if geo == "eu"
        else None
    )
    if geography is not None and geography.conflict:
        if recall_mode_active:
            if soft_reasons is not None:
                soft_reasons.append("non_eu_remote")
            logger.debug(
                "prefilter soften reason=non_eu_remote role_score=%s eligibility_hint=%s title=%s",
                role_score,
                eligibility_hint,
                (job.title or "")[:100],
            )
        else:
            logger.debug(
                "prefilter reject reason=non_eu_remote role_score=%s eligibility_hint=%s title=%s",
                role_score,
                eligibility_hint,
                (job.title or "")[:100],
            )
            return False, "non_eu_remote"
    logger.debug(
        "prefilter pass role_score=%s ambiguous=%s remote_conflict=%s title=%s",
        role_score,
        role_ambiguous,
        remote_conflict,
        (job.title or "")[:100],
    )
    return True, None
