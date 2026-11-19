from __future__ import annotations

from ultron.core.models import ClassificationSignals, ExtractedJob, ExtractionMeta, JobEvaluation
from ultron.core.rules.term_matching import find_terms
from ultron.core.vocabulary import EMPTY_PACK, VocabularyPack

KEEP_BASE_SCORE = 85
ELIGIBILITY_DISCARD_SCORE = 62
OTHER_DISCARD_SCORE = 50
ROLE_DISCARD_SCORE = 30
CLOSED_DISCARD_SCORE = 6
SIGNAL_SCORE = 10
CONFLICT_SCORE = 16
ROLE_TEXT_SCORE = 15


class AmbiguityScorer:
    """Calculates the existing 0..100 agent-review gate score.

    Role-family text signals come from the active vocabulary pack (none with an empty pack).
    """

    def __init__(self, vocabulary: VocabularyPack = EMPTY_PACK) -> None:
        self.vocabulary = vocabulary

    def score(
        self,
        job: ExtractedJob,
        fallback: JobEvaluation,
        *,
        extraction_meta: ExtractionMeta | None = None,
        classification_signals: ClassificationSignals | None = None,
    ) -> int:
        if fallback.keep:
            score = KEEP_BASE_SCORE
            if classification_signals and classification_signals.role_ambiguous:
                score += 6
            if classification_signals and classification_signals.remote_conflict:
                score += 8
            return self._clamp(score)
        score = self._category_score(fallback.discard_category)
        low_text = f"{job.title}\n{job.remote_text}\n{job.body_text[:2600]}".lower()
        score += self._text_signal_score(low_text, fallback.discard_category, self.vocabulary)
        if not fallback.eligibility:
            score += SIGNAL_SCORE
        if len(job.body_text or "") > 9000:
            score += 4
        score += self._classification_score(classification_signals, fallback.discard_category, low_text)
        if extraction_meta is not None and extraction_meta.field_conflicts:
            score += min(12, 4 * len(extraction_meta.field_conflicts))
        return self._clamp(score)

    @staticmethod
    def _category_score(category: str | None) -> int:
        return {
            "eligibility_scope": ELIGIBILITY_DISCARD_SCORE,
            "remote_policy": ELIGIBILITY_DISCARD_SCORE,
            "seniority": ELIGIBILITY_DISCARD_SCORE,
            "other": OTHER_DISCARD_SCORE,
            "role_family": ROLE_DISCARD_SCORE,
            "closed_or_invalid": CLOSED_DISCARD_SCORE,
        }.get(category or "other", 0)

    @staticmethod
    def _text_signal_score(low_text: str, category: str | None, vocabulary: VocabularyPack = EMPTY_PACK) -> int:
        score = 0
        if find_terms(low_text, ("timezone", "time zone", "cet", "cest", "emea")):
            score += SIGNAL_SCORE
        if find_terms(low_text, ("remote",)) and find_terms(low_text, ("hybrid", "onsite", "on-site")):
            score += CONFLICT_SCORE
        if category == "role_family":
            title_low = low_text.split("\n", 1)[0]
            signals = vocabulary.ambiguity_text_signals
            if not find_terms(title_low, vocabulary.role_terms) and find_terms(low_text, signals):
                score += ROLE_TEXT_SCORE
        return score

    @staticmethod
    def _classification_score(signals: ClassificationSignals | None, category: str | None, low_text: str) -> int:
        if signals is None:
            return 0
        score = 0
        if signals.role_ambiguous:
            score += 18
        if signals.remote_conflict is True and find_terms(low_text, ("remote",)):
            score += 12
        if signals.seniority_ambiguous:
            score += 10
        if signals.role_score >= 0 and category == "role_family":
            score += 18
        return score

    @staticmethod
    def _clamp(value: int) -> int:
        return max(0, min(100, value))
