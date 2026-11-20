from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from ultron.core.decision.evaluation_profile import EvaluationProfile
from ultron.core.models import ClassificationSignals, ExtractedJob, ExtractionMeta, GeoScope, JobEvaluation
from ultron.core.rules.geography import (
    is_european_scope,
    location_scope,
    scoped_eligibility_terms,
    scoped_posting_eligibility,
)
from ultron.core.rules.hard_filters import (
    classify_eligibility,
    is_full_remote_only,
    is_open_status_ok,
    location_excluded,
)
from ultron.core.rules.hard_filters_data import (
    CLOSED_STATUS_PHRASES,
    DISALLOWED_REMOTE_TERMS,
    HIGH_SENIORITY_IC_TERMS,
    JUNIOR_TITLE_TERMS_WB,
    ONSITE_TERMS,
    REMOTE_HINT_TERMS,
    ROLE_FAMILY_PASS_SCORE,
    SENIOR_REQUIRED_TERMS,
    STUDENT_LEVEL_TITLE_TERMS,
    STUDENT_LEVEL_TRIGGER_TERMS,
)
from ultron.core.rules.role_matching import (
    DEFAULT_ROLE_NEGATIVES,
    RoleNegatives,
    is_allowed_family,
    is_senior_title,
)
from ultron.core.rules.term_matching import extract_matching_evidence, find_terms, normalize_text
from ultron.core.vocabulary import EMPTY_PACK, VocabularyPack

logger = logging.getLogger(__name__)

_SPANISH_REMOTE_FORMS = {"hibrido", "hibrida", "hibridos", "hibridas", "presenciales"}
# The French and Dutch internship marker leads the title and is followed by a separator or a preposition
# ("Stage - Data Analyst", "Stage: ...", "Stage en finance", "Stage d'analyste", "Stage bij ..."). English
# occupations ("Stage Manager", "Stage Technician") are not internships.
_LEADING_STAGE = re.compile(
    r"\s*stage(?:\s*[-–—:|(]|\s+(?:en|de|du|des|bij|in|op)\b|\s+d['’])", re.IGNORECASE
)
_EMPLOYMENT_LEVEL_FIELD = re.compile(
    r"^[ \t]*(?:employment type|experience level)[ \t]*:[ \t]*(?:\r?\n[ \t]*)?([^\r\n]+)",
    re.IGNORECASE | re.MULTILINE,
)


def _specialty_pattern(term: str) -> re.Pattern[str]:
    """Match a specialty at a word start, dropping at most one plural "s".

    Stems of three characters or fewer ("Go", "R", "CSS", "AWS") must be whole words with an optional "s";
    longer stems may also prefix a word ("agents" matches "agentes", "eval" matches "evaluation").
    """
    stem = normalize_text(term)
    if len(stem) > 3 and stem.endswith("s"):
        stem = stem[:-1]
    tail = r"s?(?!\w)" if len(stem) <= 3 else ""
    return re.compile(r"(?<!\w)" + re.escape(stem) + tail)


@dataclass(slots=True)
class HeuristicConfig:
    """ULTRON replacement for the heuristic slice of MARK III's ``EvaluatorConfig``.

    Defaults are MARK III's. ``evaluation_profile`` is derived from the snapshot exactly as
    MARK III's ``JobEvaluator.__init__`` did; ``recall_mode_active`` is read by ``DecisionFinalizer``.
    ``vocabulary`` is the profile's active vocabulary pack (role-family defaults).
    ``geo`` is the profile's geographic scope: ``None`` or ``"eu"`` keep MARK III's
    behaviour; ``"other"``/``"global"`` use only the profile's ``location_scope`` terms
    (``eligibility_terms`` plus ``preferred`` regions, ``excluded_terms``).
    ``role_negatives`` are the profile-aware non-family penalty terms.
    """

    role_focus_terms: tuple[str, ...] = ()
    search_profile_snapshot: dict | None = None
    evaluation_profile: EvaluationProfile | None = None
    recall_mode_active: bool = False
    vocabulary: VocabularyPack = EMPTY_PACK
    geo: GeoScope | None = None
    role_negatives: RoleNegatives = DEFAULT_ROLE_NEGATIVES

    def __post_init__(self) -> None:
        if self.evaluation_profile is None and self.search_profile_snapshot:
            self.evaluation_profile = EvaluationProfile.from_search_profile(self.search_profile_snapshot)


class HeuristicEvaluator:
    """Builds the deterministic evaluator baseline without changing policy."""

    def __init__(self, cfg: HeuristicConfig) -> None:
        self.cfg = cfg

    def evaluate(
        self,
        job: ExtractedJob,
        *,
        extraction_meta: ExtractionMeta | None = None,
        classification_signals: ClassificationSignals | None = None,
    ) -> JobEvaluation:
        text = f"{job.remote_text}\n{job.body_text}"
        is_senior, allowed_family = self._role_signals(job, classification_signals)
        full_remote = is_full_remote_only(job.remote_text, job.body_text)
        if classification_signals and classification_signals.remote_conflict is True:
            full_remote = False
        open_status = is_open_status_ok(
            job.body_text, job.apply_available, job.is_open_hint, expired=job.valid_through_expired
        )
        eligibility = self._eligibility(job, classification_signals)
        fit_signals = self._fit_signals(text)
        verdict, reason, category = self._decision(
            allowed_family, is_senior, full_remote, open_status, classification_signals
        )
        if verdict != "discard" and (self._excluded_optional_level(job) or self._excluded_student_level(job)):
            verdict, reason, category = "discard", "seniority_not_senior", "seniority"
        if verdict != "discard" and self._location_excluded(job):
            verdict, reason, category = "discard", "location_excluded", "eligibility_scope"
        evaluation = JobEvaluation(
            keep=verdict == "keep",
            discard_reason=reason,
            verdict=verdict,
            discard_category=category,
            confidence="medium",
            evaluation_source="heuristic_fallback",
            is_senior=is_senior,
            is_allowed_family=allowed_family,
            is_full_remote=full_remote,
            eligibility=eligibility,
            eligibility_evidence=extract_matching_evidence(text, self._eligibility_evidence_terms()),
            fit_signals=fit_signals,
            open_status_ok=open_status,
            seniority_evidence=extract_matching_evidence(
                job.title,
                self._seniority_evidence_terms(),
            ),
            remote_evidence=extract_matching_evidence(
                job.remote_text, REMOTE_HINT_TERMS | DISALLOWED_REMOTE_TERMS | ONSITE_TERMS | _SPANISH_REMOTE_FORMS
            )
            or extract_matching_evidence(
                job.body_text, REMOTE_HINT_TERMS | DISALLOWED_REMOTE_TERMS | ONSITE_TERMS | _SPANISH_REMOTE_FORMS
            ),
            role_evidence=extract_matching_evidence(text, self._role_terms()),
            open_status_evidence=extract_matching_evidence(
                job.body_text, {"apply", "accepting applications"} | CLOSED_STATUS_PHRASES
            ),
            raw_provider_payload={},
        )
        self._log_signals(job, classification_signals, extraction_meta)
        return evaluation

    def _role_signals(self, job: ExtractedJob, signals: ClassificationSignals | None) -> tuple[bool, bool]:
        profile = getattr(self.cfg, "evaluation_profile", None)
        senior = is_senior_title(
            job.title,
            allowed_terms=self.cfg.role_focus_terms,
            level_terms=getattr(profile, "seniority_preferred_terms", ()) or None,
            excluded_level_terms=getattr(profile, "seniority_excluded_terms", ()) or None,
        )
        allowed = is_allowed_family(
            job.title,
            job.body_text,
            allowed_terms=self.cfg.role_focus_terms,
            strong_signal_terms=getattr(profile, "role_signal_terms", None),
            data_scientist_signal_terms=getattr(profile, "data_scientist_signal_terms", None),
            vocabulary=self.cfg.vocabulary,
            negatives=getattr(self.cfg, "role_negatives", DEFAULT_ROLE_NEGATIVES),
        )
        if signals is not None and int(signals.role_score) >= ROLE_FAMILY_PASS_SCORE:
            allowed = True
        return senior, allowed

    def _excluded_optional_level(self, job: ExtractedJob) -> bool:
        profile = self.cfg.evaluation_profile
        if profile is None or profile.seniority != "any" or not profile.seniority_excluded_terms:
            return False
        evidence = "\n".join((job.title, *_EMPLOYMENT_LEVEL_FIELD.findall(job.body_text)))
        return bool(find_terms(evidence, profile.seniority_excluded_terms))

    def _excluded_student_level(self, job: ExtractedJob) -> bool:
        """A student or internship title, for a profile whose ``seniority.excluded_terms`` excludes interns."""
        profile = self.cfg.evaluation_profile
        if profile is None or not any(
            normalize_text(term) in STUDENT_LEVEL_TRIGGER_TERMS for term in profile.seniority_excluded_terms
        ):
            return False
        if find_terms(job.title, STUDENT_LEVEL_TITLE_TERMS):
            return True
        # A profile that targets a stage occupation ("stage manager") never reads its own title as an internship.
        roles = (*self.cfg.role_focus_terms, *profile.role_terms)
        stage_roles = [term for term in roles if find_terms(term, ("stage",))]
        return bool(_LEADING_STAGE.match(job.title or "")) and not find_terms(job.title, stage_roles)

    def _seniority_evidence_terms(self) -> set[str]:
        profile = getattr(self.cfg, "evaluation_profile", None)
        preferred = getattr(profile, "seniority_preferred_terms", ())
        excluded = getattr(profile, "seniority_excluded_terms", ())
        return {
            term.strip()
            for term in (preferred or tuple(SENIOR_REQUIRED_TERMS | HIGH_SENIORITY_IC_TERMS))
            + (excluded or tuple(JUNIOR_TITLE_TERMS_WB))
        }

    def _scoped(self) -> bool:
        """True for a non-European profile scope (``"other"``/``"global"``)."""
        return getattr(self.cfg, "geo", None) in {"other", "global"}

    def _location_excluded(self, job: ExtractedJob) -> bool:
        """Non-European scopes: the posting names one of ``location_scope.excluded_terms``."""
        if not self._scoped():
            return False
        excluded = location_scope(self.cfg.search_profile_snapshot)["excluded_terms"]
        return location_excluded(f"{job.location}\n{job.remote_text}\n{job.body_text}", excluded)

    def _eligibility(self, job: ExtractedJob, signals: ClassificationSignals | None) -> str | None:
        if self._scoped():
            if signals is not None:
                return signals.eligibility_hint
            terms = scoped_eligibility_terms(self.cfg.search_profile_snapshot)
            return scoped_posting_eligibility(job.location, job.remote_text, job.body_text, terms)
        text = f"{job.remote_text}\n{job.body_text}"
        profile = getattr(self.cfg, "evaluation_profile", None)
        regions = tuple(getattr(profile, "target_regions", ()) or ())
        if not regions:
            return None
        if not is_european_scope(regions):
            normalized = text.casefold()
            for region in regions:
                term = str(region).casefold().removeprefix("remote ").strip()
                if term and term in normalized:
                    return "high"
            return "medium" if "remote" in normalized else None
        eligibility = (
            signals.eligibility_hint if signals and signals.eligibility_hint is not None else classify_eligibility(text)
        )
        if signals and signals.remote_scope_hint and eligibility is None:
            return {
                "remote_spain": "confirmed",
                "remote_eu": "high",
                "remote_europe": "high",
            }.get(signals.remote_scope_hint, "medium")
        return eligibility

    def _fit_signals(self, text: str) -> list[str]:
        found: list[str] = []
        low = normalize_text(text)
        profile = getattr(self.cfg, "evaluation_profile", None)
        specialties = tuple(getattr(profile, "specialties", ()) or ())
        for specialty in specialties:
            normalized = str(specialty).strip()
            if normalized and normalized not in found and _specialty_pattern(normalized).search(low):
                found.append(normalized)
            if len(found) == 3:
                break
        return found

    def _role_terms(self) -> tuple[str, ...]:
        profile = getattr(self.cfg, "evaluation_profile", None)
        return tuple(getattr(profile, "role_terms", ()) or self.cfg.role_focus_terms or ())

    def _eligibility_evidence_terms(self) -> list[str]:
        profile = getattr(self.cfg, "evaluation_profile", None)
        regions = tuple(getattr(profile, "target_regions", ()) or ())
        return [str(region).casefold().removeprefix("remote ").strip() for region in regions if str(region).strip()]

    def _decision(
        self, allowed: bool, senior: bool, remote: bool, open_status: bool, signals: ClassificationSignals | None
    ) -> tuple[str, str | None, str | None]:
        profile = getattr(self.cfg, "evaluation_profile", None)
        senior_required = getattr(profile, "seniority", "senior") != "any"
        remote_required = getattr(profile, "remote_policy", "full_remote") == "full_remote"
        senior_ok = senior or not senior_required
        remote_ok = remote or not remote_required
        keep = allowed and senior_ok and remote_ok and open_status
        reason_category = (
            ("non_matching_role_family", "role_family")
            if not allowed
            else ("seniority_not_senior", "seniority")
            if not senior_ok
            else ("hybrid_or_not_explicit_remote", "remote_policy")
            if not remote_ok
            else ("expired_or_closed", "closed_or_invalid")
            if not open_status
            else ("rule_violation", "other")
        )
        if signals and not senior_ok and signals.seniority_ambiguous and allowed and remote_ok and open_status:
            return "unsure", None, None
        return ("keep", None, None) if keep else ("discard", *reason_category)

    @staticmethod
    def _log_signals(
        job: ExtractedJob, signals: ClassificationSignals | None, extraction_meta: ExtractionMeta | None
    ) -> None:
        if signals is not None:
            logger.debug(
                "evaluator heuristic role_score=%s role_ambiguous=%s title=%s",
                signals.role_score,
                bool(signals.role_ambiguous),
                (job.title or "")[:100],
            )
        if extraction_meta is not None:
            logger.debug(
                "evaluator extract_meta title=%s/%s company=%s/%s location=%s/%s remote=%s/%s conflicts=%s",
                extraction_meta.title_source,
                extraction_meta.title_confidence,
                extraction_meta.company_source,
                extraction_meta.company_confidence,
                extraction_meta.location_source,
                extraction_meta.location_confidence,
                extraction_meta.remote_source,
                extraction_meta.remote_confidence,
                ",".join(extraction_meta.field_conflicts or []),
            )
