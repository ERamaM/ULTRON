"""Role-family scoring and seniority matching over explicit profile vocabulary."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from ultron.core.rules import hard_filters_data as rules_data
from ultron.core.rules.term_matching import find_terms, normalize_text
from ultron.core.rules.text_patterns import contains_any_substring, contains_any_word_boundary
from ultron.core.rules.title_synonyms import expand_role_terms, normalize_role_title, normalize_title_separators
from ultron.core.vocabulary import EMPTY_PACK, VocabularyPack


@dataclass(frozen=True, slots=True)
class RoleNegatives:
    """Non-family penalty terms (title and body) in effect for one profile.

    The defaults are MARK III's lists. ``role_negatives_for`` drops the terms the
    profile itself targets, so a finance or sales profile is not penalized for its
    own vocabulary. Computed once per profile and passed down explicitly.
    """

    title_terms: frozenset[str] = frozenset(rules_data.ROLE_FAMILY_NEGATIVE_TERMS_WB)
    body_terms: frozenset[str] = frozenset(rules_data.ROLE_NEGATIVE_BODY_TERMS)


DEFAULT_ROLE_NEGATIVES = RoleNegatives()
_ROLE_NEGATIVE_SOURCES = ("role_focus_terms", "target_titles", "role_signal_terms")


def _prefix_stem(term: str) -> str:
    """``term`` without a final "e" (finance -> financ), so suffixed forms match as prefixes."""
    return term[:-1] if term.endswith("e") and len(term) > 4 else term


def role_negatives_for(profile: Mapping[str, Any] | None) -> RoleNegatives:
    """Drop every negative term that starts a word (any case) of the profile's role terms.

    Prefix match (ruling P2-R6): "account" is unlocked by "Accountant"/"accounting". A
    final silent "e" is dropped before matching (English suffixes replace it), so
    "finance" is unlocked by "Financial"/"financing". A multi-word negative must start
    at a word boundary.

    Sources: ``criteria.role_focus_terms``, ``target_titles`` and ``role_signal_terms``.
    """
    criteria = (profile or {}).get("criteria")
    criteria = criteria if isinstance(criteria, Mapping) else {}
    corpus = "\n".join(
        str(term)
        for key in _ROLE_NEGATIVE_SOURCES
        if isinstance(criteria.get(key), (list, tuple))
        for term in criteria[key]
    )
    if not corpus.strip():
        return DEFAULT_ROLE_NEGATIVES

    def _unlocked(term: str) -> bool:
        return re.search(r"\b" + re.escape(_prefix_stem(term)), corpus, re.IGNORECASE) is not None

    def _kept(terms: frozenset[str]) -> frozenset[str]:
        return frozenset(term for term in terms if not _unlocked(term))

    return RoleNegatives(
        title_terms=_kept(DEFAULT_ROLE_NEGATIVES.title_terms),
        body_terms=_kept(DEFAULT_ROLE_NEGATIVES.body_terms),
    )


def role_ambiguity_detected(
    role_score: int,
    role_text: str,
    role_ambiguity_terms: Iterable[str] | None = None,
    *,
    vocabulary: VocabularyPack = EMPTY_PACK,
) -> bool:
    """Return whether a borderline role has a configured specialist signal.

    Without profile terms, the active vocabulary pack's ``ambiguity_terms`` apply.
    """

    terms = [str(term).casefold() for term in (role_ambiguity_terms or ()) if str(term).strip()]
    if not terms:
        terms = list(vocabulary.ambiguity_terms)
    low_text = str(role_text or "").casefold()
    return rules_data.ROLE_FAMILY_CLEAR_REJECT_SCORE < role_score < rules_data.ROLE_FAMILY_PASS_SCORE and any(
        term in low_text for term in terms
    )


def _role_focus_words(allowed_terms: Iterable[str] | None) -> set[str]:
    """Return the lowercase word tokens that make up the user's role focus.

    Used to suppress negative-term penalties whose own words overlap with the
    user's intent. E.g., for `role_focus_terms=["marketing manager"]` we drop
    "marketing" and "manager" from the AI/ML-centric negative lists so a
    marketing search isn't sabotaged by its own keywords.
    """
    if not allowed_terms:
        return set()
    words: set[str] = set()
    for term in allowed_terms:
        words.update(re.findall(r"\w+", str(term or "").lower()))
    return words


def filter_negatives_against_focus(
    negative_terms: Iterable[str],
    role_focus_words: set[str],
) -> set[str]:
    """Drop negatives that share any word with the user's role focus."""
    if not role_focus_words:
        return set(negative_terms)
    kept: set[str] = set()
    for term in negative_terms:
        term_words = set(re.findall(r"\w+", str(term).lower()))
        if term_words & role_focus_words:
            continue
        kept.add(term)
    return kept


_NUMERIC_GRADE = re.compile(r"l?\d+")
_ROMANCE_ROLE_ENDINGS = ("ero", "era", "dor", "dora", "ista", "ante", "ente", "eur", "euse", "ier", "iere", "ico", "ica")


def _is_level_word(word: str) -> bool:
    """A level word ("senior", "ii") or a numeric grade ("2", "l4")."""
    return word in rules_data.ROLE_LEVEL_WORDS or bool(_NUMERIC_GRADE.fullmatch(word))


def role_family_nouns(allowed_terms: Iterable[str] | None) -> set[str]:
    """The family noun of each role term: its last word once trailing level words are dropped.

    "Senior Data Analyst" -> "analyst", "clinical nurse" -> "nurse", "design lead" -> "design". A level word
    ("senior") or a qualifier ("clinical", "data") shared with a title does not make it the same family.
    """
    nouns: set[str] = set()
    for term in allowed_terms or ():
        # Each part of a compound term ("Occupational Therapist - Graduate Program", "Engineer, Backend") has its
        # own head.
        for part in re.split(r"\s[-–—|]\s|[,;/()]", str(term or "").lower()):
            words = re.findall(r"\w+", part)
            # Romance titles name the role before its field ("ingeniero de software", "analista de datos").
            preposition = next((i for i, word in enumerate(words) if word in rules_data.ROLE_FIELD_PREPOSITIONS), None)
            if preposition:
                words = words[:preposition]
            while len(words) > 1 and _is_level_word(words[-1]):
                words.pop()
            if words and len(words[-1]) >= 3 and not _is_level_word(words[-1]):
                nouns.add(words[-1])
            # Spanish, Portuguese and French put the role first ("Diseñador UX", "Desarrolladora Backend").
            first = normalize_text(words[0]) if len(words) > 1 else ""
            if len(first) >= 4 and not _is_level_word(first) and first.endswith(_ROMANCE_ROLE_ENDINGS):
                nouns.add(words[0])
    return nouns


# Gendered and plural endings, replaced longest first so every form of one noun has the same stem:
# German -in/-innen (Entwicklerin, Ingenieurin), French -euse/-rice/-ière (développeuse, directrice, infirmière),
# Spanish and Portuguese -ora/-oras/-ores (desarrolladora), then a plural -s/-es and one final vowel
# (ingeniero/ingeniera/ingenieros, analista/analistas, nurse/nurses).
_GENDER_ENDINGS = (
    ("erinnen", "er"), ("eurinnen", "eur"), ("erin", "er"), ("eurin", "eur"), ("euses", "eur"), ("euse", "eur"),
    ("trici", "tor"), ("trice", "tor"), ("rices", "eur"), ("rice", "eur"), ("ieres", "ier"), ("iere", "ier"), ("oras", "or"), ("ores", "or"),
    ("ora", "or"),
)


def _noun_stem(word: str) -> str:
    """One stem for the gender and plural forms of a role noun (accents folded)."""
    stem = normalize_text(word)
    for ending, replacement in _GENDER_ENDINGS:
        if stem.endswith(ending) and len(stem) - len(ending) >= 3:
            stem = stem[: -len(ending)] + replacement
            break
    else:
        if stem.endswith("s") and len(stem) > 4:
            stem = stem[:-1]
        if stem[-1:] in {"a", "e", "o"} and len(stem) > 4:
            stem = stem[:-1]
    # French -eur and Italian -tore/-trice meet the Spanish and English -or (directeur, direttore, director).
    return stem[:-3] + "or" if stem.endswith("eur") and len(stem) > 5 else stem


def _title_has_family_noun(title_low: str, nouns: set[str]) -> bool:
    """True when a title word is the family noun or one of its gender or plural forms.

    Compound German titles ("Softwareentwicklerin") name the noun at the end of the word.
    """
    stems = {_noun_stem(noun) for noun in nouns}
    for word in re.findall(r"\w+", normalize_text(title_low)):
        word_stem = _noun_stem(word)
        if word_stem in stems or any(len(stem) >= 6 and word_stem.endswith(stem) for stem in stems):
            return True
    return False


def is_senior_title(
    title: str,
    allowed_terms: Iterable[str] | None = None,
    *,
    level_terms: Iterable[str] | None = None,
    excluded_level_terms: Iterable[str] | None = None,
) -> bool:
    """Match configured levels, retaining senior defaults when unspecified.

    Role focus exempts explicitly targeted management titles from disallowed terms.
    """
    title_low = title.lower()
    has_level = (
        bool(find_terms(title, level_terms))
        if level_terms is not None
        else contains_any_substring(title_low, rules_data.SENIOR_REQUIRED_TERMS)
        or contains_any_word_boundary(title_low, rules_data.HIGH_SENIORITY_IC_TERMS)
    )
    if not has_level:
        return False

    focus_words = _role_focus_words(allowed_terms)
    effective_wb_disallowed = filter_negatives_against_focus(rules_data.DISALLOWED_TITLE_TERMS_WB, focus_words)
    effective_substr_disallowed = filter_negatives_against_focus(rules_data.DISALLOWED_TITLE_TERMS_SUBSTR, focus_words)

    if effective_wb_disallowed and contains_any_word_boundary(title_low, effective_wb_disallowed):
        return False
    if effective_substr_disallowed and contains_any_substring(title_low, effective_substr_disallowed):
        return False
    excluded = (
        find_terms(title, excluded_level_terms)
        if excluded_level_terms
        else contains_any_word_boundary(title_low, rules_data.JUNIOR_TITLE_TERMS_WB)
    )
    return not excluded


def _normalized_role_terms(
    allowed_terms: Iterable[str] | None, vocabulary: VocabularyPack = EMPTY_PACK
) -> set[str]:
    if not allowed_terms:
        return set(vocabulary.role_terms)
    terms = {str(term).strip().lower() for term in allowed_terms if str(term).strip()}
    if not terms:
        return set(vocabulary.role_terms)
    return terms


def score_role_family(
    title: str,
    body_text: str,
    allowed_terms: Iterable[str] | None = None,
    strong_signal_terms: Iterable[str] | None = None,
    data_scientist_signal_terms: Iterable[str] | None = None,
    *,
    vocabulary: VocabularyPack = EMPTY_PACK,
    negatives: RoleNegatives = DEFAULT_ROLE_NEGATIVES,
) -> tuple[int, list[str]]:
    """Score role fit and return the explanatory signal names.

    ``allowed_terms`` changes the role vocabulary; the returned score is a
    heuristic signal for ranking, not a final keep/discard decision.
    """
    raw_terms = tuple(allowed_terms or ())
    if allowed_terms:
        allowed_terms = expand_role_terms(allowed_terms, extra_groups=vocabulary.title_synonym_groups)
    t = (title or "").lower()
    b = (body_text or "").lower()
    score = 0
    signals: list[str] = []
    role_terms = _normalized_role_terms(allowed_terms, vocabulary)
    custom_role_terms = bool(allowed_terms) and role_terms != set(vocabulary.role_terms)
    if custom_role_terms and not vocabulary.role_terms:
        title_exact_hits = find_terms(normalize_role_title(t), sorted(role_terms))
    else:
        normalized_title = normalize_role_title(t)
        title_exact_hits = [term for term in role_terms if normalize_role_title(term) in normalized_title]
    custom_exact_title_match = custom_role_terms and bool(title_exact_hits)

    # Suppress negative-terms penalties whose own words overlap with the user's
    # role focus. e.g., for `role_focus_terms=["marketing manager"]` we don't
    # penalize titles for containing "marketing" or "manager"; for an AI/ML
    # default profile nothing is suppressed and original behavior holds.
    focus_words = _role_focus_words(allowed_terms) if custom_role_terms else set()
    effective_management = filter_negatives_against_focus(rules_data.ROLE_FAMILY_MANAGEMENT_TERMS_WB, focus_words)
    effective_lead_substr = filter_negatives_against_focus(rules_data.DISALLOWED_TITLE_TERMS_SUBSTR, focus_words)
    effective_non_tech = filter_negatives_against_focus(negatives.title_terms, focus_words)
    effective_negative_body = filter_negatives_against_focus(negatives.body_terms, focus_words)

    if (vocabulary.role_terms or not custom_exact_title_match) and contains_any_word_boundary(
        t, rules_data.JUNIOR_TITLE_TERMS_WB
    ):
        score -= 3
        signals.append("junior_title")

    if effective_management and contains_any_word_boundary(t, effective_management) and not custom_exact_title_match:
        score -= 6
        signals.append("management_title")

    if effective_lead_substr and contains_any_substring(t, effective_lead_substr) and not custom_exact_title_match:
        score -= 4
        signals.append("lead_manager_title")

    if effective_non_tech and contains_any_word_boundary(t, effective_non_tech) and not custom_exact_title_match:
        score -= 5
        signals.append("non_tech_title")

    if title_exact_hits:
        best = max(title_exact_hits, key=len)
        if "data scientist" in best:
            score += 2
            signals.append("title:data_scientist")
        else:
            score += 4
            signals.append(f"title:{best[:30]}")

    effective_strong_signals = {
        str(term).strip().casefold() for term in (strong_signal_terms or allowed_terms or ()) if str(term).strip()
    }
    # Without a vocabulary pack, body signals only count for a title that names the profile's family noun
    # (from role_focus_terms / target_titles): signal terms alone never pass an off-family title. When no family
    # noun can be derived from the profile ("QA", "L4"), the rule does not apply and the earlier, looser check
    # (a bridge title or any role word of four letters or more) is kept.
    family_nouns = role_family_nouns((*raw_terms, *(allowed_terms or ())))
    if family_nouns:
        title_names_family = _title_has_family_noun(normalize_role_title(t), family_nouns)
    else:
        focus_tokens = _role_focus_words(allowed_terms)
        title_names_family = any(term in t for term in rules_data.ROLE_BRIDGE_TITLE_TERMS) or any(
            len(word) >= 4 and word in focus_tokens for word in re.findall(r"\w+", t)
        )
    has_role_noun = bool(score >= 0 and custom_role_terms and not vocabulary.role_terms and title_names_family)
    title_strong_hit = any(term in t for term in effective_strong_signals)
    normalized_body = normalize_title_separators(b)
    raw_body_hits = [
        term for term in effective_strong_signals
        if normalize_title_separators(term) in normalized_body
    ]
    allow_custom_body = has_role_noun and (title_strong_hit or len(raw_body_hits) >= 2)
    body_strong_hits = (
        []
        if custom_role_terms and not custom_exact_title_match and not allow_custom_body
        else raw_body_hits
    )
    if body_strong_hits:
        score += min(4, 1 + len(body_strong_hits))
        signals.append("body:ai_ml_signals")

    # Without a pack, a bridge title ("Software Engineer, Analytics Platform") outside the profile's family earns
    # no bonus from a signal term: only a title with the family noun or an exact role term does.
    packless_off_family = (
        custom_role_terms and not vocabulary.role_terms and not custom_exact_title_match and not has_role_noun
    )
    bridge_title = [term for term in rules_data.ROLE_BRIDGE_TITLE_TERMS if term in t and not packless_off_family]
    if bridge_title and (body_strong_hits or any(term in t for term in effective_strong_signals)):
        score += 2
        signals.append("bridge_title+ai")

    if "data scientist" in t:
        effective_ds_signals = {
            str(term).strip().casefold() for term in (data_scientist_signal_terms or ()) if str(term).strip()
        }
        if effective_ds_signals and contains_any_substring(b, effective_ds_signals):
            score += 2
            signals.append("ds_ml_body")
        elif contains_any_substring(b, vocabulary.non_family_signals):
            # Only penalize analytics-only signals when there are NO ML signals
            # in the body; genuine ML DS roles often mention dashboards too.
            score -= 4
            signals.append("ds_analytics_only")

    if effective_negative_body and any(term in b for term in effective_negative_body):
        if not custom_exact_title_match:
            score -= 3
            signals.append("negative_body")

    if not any(term in t for term in vocabulary.title_anchor_terms) and body_strong_hits:
        signals.append("title_ambiguous_body_positive")

    return score, signals[:6]


def is_allowed_family(
    title: str,
    body_text: str,
    allowed_terms: Iterable[str] | None = None,
    strong_signal_terms: Iterable[str] | None = None,
    data_scientist_signal_terms: Iterable[str] | None = None,
    *,
    vocabulary: VocabularyPack = EMPTY_PACK,
    negatives: RoleNegatives = DEFAULT_ROLE_NEGATIVES,
) -> bool:
    score, _signals = score_role_family(
        title,
        body_text,
        allowed_terms=allowed_terms,
        strong_signal_terms=strong_signal_terms,
        data_scientist_signal_terms=data_scientist_signal_terms,
        vocabulary=vocabulary,
        negatives=negatives,
    )
    return score >= rules_data.ROLE_FAMILY_PASS_SCORE
