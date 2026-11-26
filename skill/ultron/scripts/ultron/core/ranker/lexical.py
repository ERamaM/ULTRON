"""Deterministic BM25F-style pre-ranking of factual job dossiers."""

from collections import Counter
from datetime import UTC, date, datetime
from math import log1p

from ultron.core.rules.geography import location_scope
from ultron.core.rules.term_matching import normalize_text
from ultron.core.text_clean import clean_text
from ultron.core.web_search.query_builder import work_mode_term

MAX_PER_COMPANY = 3
_FIELDS = {"title": (5.0, 6), "location": (1.8, 5), "remote": (1.0, 4),
           "geo_excerpt": (1.1, 28), "role_excerpt": (0.9, 90), "snippet": (0.65, 38)}


def _tokens(text: str) -> list[str]:
    return "".join(char if char.isalnum() else " " for char in normalize_text(text)).split()


def _values(criteria: dict, *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        raw = criteria.get(name)
        items = raw if isinstance(raw, list | tuple) else [raw]
        values.extend(clean_text(item)[:200] for item in items[:40] if isinstance(item, str))
    return [value for value in values if value]


def _fresh(posted: object, today: date) -> float:
    try:
        age = (today - date.fromisoformat(str(posted)[:10])).days
    except ValueError:
        return 0.0
    if 0 <= age <= 14:
        return 0.35
    return 0.15 if 14 < age <= 30 else 0.0


def _field_weights(dossier: dict) -> dict[str, float]:
    weights: dict[str, float] = {}
    for name, (boost, typical_length) in _FIELDS.items():
        tokens = _tokens(str(dossier.get(name) or "")[:800])
        scale = boost / (0.65 + 0.35 * len(tokens) / typical_length)
        for token, count in Counter(tokens).items():
            weights[token] = weights.get(token, 0.0) + scale * count
    return weights


class LexicalRanker:
    """Use role and profile terms to order dossiers without making eligibility decisions."""

    def __init__(self, profile: dict, *, query: str = "", today: date | None = None) -> None:
        criteria = profile.get("criteria") or {}
        titles = [query] if query else _values(criteria, "target_titles", "role_focus_terms")
        body = _values(criteria, "specialties", "must_have_terms", "role_signal_terms", "work_mode")
        body.extend((*location_scope(profile)["preferred"], work_mode_term(profile)))
        self.roles = tuple(dict.fromkeys(_tokens(" ".join(titles))))
        self.context = tuple(sorted(set(_tokens(" ".join(body))) - set(self.roles)))
        self.exclusions = tuple(dict.fromkeys(" ".join(_tokens(phrase)) for phrase in
                                             _values(criteria, "excluded_titles", "exclude_terms")))
        self.today = today if today is not None else datetime.now(UTC).date()

    def ordered(self, dossiers: list[dict]) -> list[dict]:
        """Return every dossier, ordered by lexical relevance and small objective boosts."""
        weighted = [_field_weights(dossier) for dossier in dossiers]
        terms = set(self.roles) | set(self.context)
        frequency = Counter(term for weights in weighted for term in weights if term in terms)
        count = len(dossiers)
        idf = {term: log1p((count - frequency[term] + 0.5) / (frequency[term] + 0.5)) for term in terms}

        def score(dossier: dict, weights: dict[str, float]) -> float:
            role = sum(idf[term] * weights[term] / (1.2 + weights[term]) for term in self.roles if term in weights)
            context = sum(idf[term] * weights[term] / (1.2 + weights[term])
                          for term in self.context if term in weights)
            title = " " + " ".join(_tokens(str(dossier.get("title") or ""))) + " "
            penalty = 1.4 * sum(1 for term in self.exclusions if term and f" {term} " in title)
            boost = (0.3 * (dossier.get("source") == "watch") + 0.2 * (dossier.get("memory") == "new")
                     + 0.2 * bool(dossier.get("board")) + _fresh(dossier.get("posted"), self.today))
            return 2.0 * role + 0.65 * context + boost - penalty

        pairs = zip(dossiers, weighted, strict=True)
        return [dossier for dossier, _ in sorted(pairs, key=lambda pair: -score(*pair))]

    def shortlist(self, dossiers: list[dict], limit: int) -> list[dict]:
        """Keep open dossiers first, with one identity and at most three roles per employer."""
        ranked = sorted(self.ordered(dossiers), key=lambda dossier: dossier.get("status") != "open")
        seen_ids: set[str] = set()
        seen_roles: set[tuple[str, str]] = set()
        companies: Counter = Counter()
        result = []
        for dossier in ranked:
            if len(result) == limit:
                break
            company = normalize_text(str(dossier.get("company") or ""))
            title = normalize_text(str(dossier.get("title") or ""))
            role = (company, title)
            identity = dossier["id"]
            if identity in seen_ids or (company and (role in seen_roles or companies[company] >= MAX_PER_COMPANY)):
                continue
            seen_ids.add(identity)
            seen_roles.add(role)
            companies[company] += 1
            result.append(dossier)
        return result
