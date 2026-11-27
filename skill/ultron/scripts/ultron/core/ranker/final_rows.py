"""Final shortlist ranking, cross-source deduplication, and title normalization."""

from __future__ import annotations

import re
import unicodedata

from ultron.core.models import COMPANY_NOT_FOUND, LOCATION_NOT_SHOWN, TITLE_NOT_FOUND, FinalRow
from ultron.core.rules.geo_eu import EU_COUNTRY_TERMS, SPAIN_LOCALITY_TERMS, US_CITY_TERMS

_TITLE_NOISE_RE = re.compile(r"\(([^)]*)\)|\[([^\]]*)\]")
_TITLE_MODE_RE = re.compile(r"\b(remote|remoto|hybrid|hibrido|teletrabajo)\b")
# Parenthetical words that only describe work mode, contract, gender tag or region, never the role itself.
_TITLE_NOISE_TOKENS = frozenset(
    {
        *("remote", "remoto", "hybrid", "hibrido", "teletrabajo", "onsite", "on", "site", "office", "fully"),
        *("full", "part", "time", "contract", "permanent", "first", "friendly", "only", "or", "and", "in"),
        *("m", "f", "w", "d", "x", "h", "mwd", "mfd", "fmd", "all", "genders", "gender"),
        *("eu", "europe", "emea", "eea", "uk", "us", "usa", "worldwide", "global", "anywhere", "latam", "apac"),
    }
)
_TITLE_PLACE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in (*EU_COUNTRY_TERMS, *SPAIN_LOCALITY_TERMS, *US_CITY_TERMS)) + r")\b"
)
_TITLE_TOKEN_SPLIT_RE = re.compile(r"[\W_]+")  # Unicode-aware: non-ASCII letters count as words too


def _replace_title_parenthetical(match: re.Match[str]) -> str:
    """Drop a parenthetical made only of work-mode/region noise; otherwise keep its words."""
    content = match.group(1) if match.group(1) is not None else match.group(2)
    tokens = _TITLE_TOKEN_SPLIT_RE.split(_TITLE_PLACE_RE.sub(" ", content))
    if all(token in _TITLE_NOISE_TOKENS for token in tokens if token):
        return " "
    return f" {re.sub(r'[,;:/|]+', ' ', content)} "
_VERDICT_RANK = {"keep": 0, "unsure": 1}
_ELIGIBILITY_RANK = {"confirmed": 0, "high": 1, "medium": 2, "unknown": 3}


def normalize_title_for_dedup(title: str) -> str:
    """Collapse title variants that only differ by mode/noise/accents."""
    folded = unicodedata.normalize("NFKD", str(title or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    low = _TITLE_NOISE_RE.sub(_replace_title_parenthetical, folded.lower())
    low = _TITLE_MODE_RE.sub(" ", low)
    return re.sub(r"\s+", " ", low).strip(" -–—|,")


class RoleIdentityIndex:
    """Own the arrival-based identities shared by capacity and final deduplication."""

    def __init__(self) -> None:
        self._urls: dict[str, str] = {}
        self._roles: dict[tuple[str, str], dict[str, str]] = {}
        self._count = 0

    @staticmethod
    def _keys(row: FinalRow) -> tuple[str, tuple[str, str] | None, str]:
        url_key = str(row.apply_url or "").strip().lower()
        company_key = str(row.company or "").strip().lower()
        title_key = normalize_title_for_dedup(row.title)
        location_key = str(row.location or "").strip().lower()
        dedup_eligible = (
            bool(company_key) and company_key != COMPANY_NOT_FOUND and bool(title_key) and title_key != TITLE_NOT_FOUND
        )
        return url_key, (company_key, title_key) if dedup_eligible else None, location_key

    def group_key(self, row: FinalRow) -> str | None:
        """Return an existing deduplication group key matching ``row``, or None."""
        url, role, location = self._keys(row)
        if url and url in self._urls:
            return self._urls[url]
        locations = self._roles.get(role) if role is not None else None
        if locations:
            if not location or location == LOCATION_NOT_SHOWN:
                return next(iter(locations.values()))
            return locations.get(location)
        return None

    def add(self, row: FinalRow) -> bool:
        """Register ``row`` and return True, or return False if already seen."""
        if self.group_key(row) is not None:
            return False
        group = str(self._count)
        self._count += 1
        url, role, location = self._keys(row)
        if url:
            self._urls[url] = group
        if role is not None:
            self._roles.setdefault(role, {})[location] = group
        return True

    @property
    def unique_count(self) -> int:
        """Return the number of distinct job groups indexed so far."""
        return self._count


# A posting the agent opened and judged itself (scout verified find) leads its duplicate group and the shortlist.
VERIFIED_FOUND_BY = "agent_verified"


def _tier(row: FinalRow) -> int:
    return 0 if row.found_by == VERIFIED_FOUND_BY else 1


def _duplicate_quality(row: FinalRow) -> tuple[int, int, int, int, float, bool]:
    # A verified find wins its group; then the agent's scout priority comes before the heuristic signals. Sweep rows
    # have no ``found_by`` and no priority, so they all tie on the first two keys and sweep results do not change.
    return (
        _tier(row),
        _VERDICT_RANK.get(row.evaluation_verdict, 2),
        row.priority if row.priority is not None else 2,
        _ELIGIBILITY_RANK.get(row.eligibility, 4),
        -row.match_score if row.match_score is not None else float("inf"),
        str(row.location or "").strip().lower() in {"", LOCATION_NOT_SHOWN},
    )


def dedupe_final_rows(
    rows: list[FinalRow], *, merge_locations: bool = False
) -> tuple[list[FinalRow], list[FinalRow]]:
    """Keep each group's best row in its first arrival's position.

    Two verified finds of the agent at different posting URLs are never merged: it opened and judged each.

    With ``merge_locations`` (remote-only profiles), the location variants of one posting (same company,
    title and board, one listing per city) also collapse into one row that lists every location.
    """
    kept: list[FinalRow] = []
    duplicates: list[FinalRow] = []
    identities = RoleIdentityIndex()
    positions: list[int] = []  # index group -> position in ``kept``
    for row in rows:
        group = identities.group_key(row)
        if group is None:
            identities.add(row)
            positions.append(len(kept))
            kept.append(row)
            continue
        at = positions[int(group)]
        if _both_verified_apart(row, kept[at]):
            kept.append(row)  # the agent opened both postings and judged each: two vacancies, not one
        elif _duplicate_quality(row) < _duplicate_quality(kept[at]):
            duplicates.append(kept[at])
            kept[at] = row
        else:
            duplicates.append(row)
    if merge_locations:
        kept, merged = _merge_location_variants(kept)
        duplicates.extend(merged)
    return kept, duplicates


def _both_verified_apart(row: FinalRow, other: FinalRow) -> bool:
    """Two verified finds at different posting URLs (same title and company, such as two openings)."""
    return (row.found_by == VERIFIED_FOUND_BY and other.found_by == VERIFIED_FOUND_BY
            and row.apply_url != other.apply_url)


_MAX_LISTED_LOCATIONS = 10


def _posting_key(row: FinalRow) -> tuple[str, str, str] | None:
    """Company, title and verified board, or ``None`` when the row may not merge.

    Only a vendor-API row names its board in ``source_key`` ("vendor:board"), queried from the vendor's own
    API. Any other row (web hits share ``source_key="web_search"``, and tenants of one ATS host share the host)
    never merges, so one site or tenant cannot displace another's posting.
    """
    _url, role, _location = RoleIdentityIndex._keys(row)
    source = str(row.source_key or "").strip().lower()
    if role is None or row.found_by == VERIFIED_FOUND_BY or row.source_type != "ats_api" or ":" not in source:
        return None
    return (*role, source)


def _listed_locations(rows: list[FinalRow]) -> str:
    locations = list(dict.fromkeys(
        str(row.location).strip() for row in rows if str(row.location or "").strip() not in {"", LOCATION_NOT_SHOWN}
    ))
    if not locations:
        return rows[0].location
    extra = len(locations) - _MAX_LISTED_LOCATIONS
    return "; ".join(locations[:_MAX_LISTED_LOCATIONS]) + (f" (+{extra} more)" if extra > 0 else "")


def _merge_location_variants(rows: list[FinalRow]) -> tuple[list[FinalRow], list[FinalRow]]:
    groups: dict[tuple[str, str, str], list[FinalRow]] = {}
    order: list[tuple[str, str, str] | int] = []
    for index, row in enumerate(rows):
        key = _posting_key(row)
        if key is None:
            order.append(index)
        elif key in groups:
            groups[key].append(row)
        else:
            groups[key] = [row]
            order.append(key)
    kept: list[FinalRow] = []
    duplicates: list[FinalRow] = []
    for item in order:
        if isinstance(item, int):
            kept.append(rows[item])
            continue
        members = groups[item]
        # Ties go to the smallest apply_url, not the first arrival, so the same row represents the posting in
        # every run whatever order the board lists the cities in (the ledger tracks it as one job).
        best = min(members, key=lambda member: (_duplicate_quality(member), str(member.apply_url)))
        if len(members) > 1:
            # In place: callers track rows by identity (finalize maps each row back to its posting).
            best.location = _listed_locations(members)
        kept.append(best)
        duplicates.extend(member for member in members if member is not best)
    return kept, duplicates


_BAND_RANK = {"strong": 0, "good": 1, "borderline": 2, "ineligible": 3, None: 2}


def rank_final_rows(rows: list[FinalRow], *, use_match_score: bool = False) -> list[FinalRow]:
    """Deterministically rank rows, optionally activating score-v1 ordering."""

    def key(row: FinalRow) -> tuple:
        if not use_match_score:
            return (
                _VERDICT_RANK.get(row.evaluation_verdict, 2),
                _ELIGIBILITY_RANK.get(row.eligibility, 4),
                0 if row.match_score is not None else 1,
                -(row.match_score or 0),
                row.company.casefold(),
                row.title.casefold(),
                row.apply_url,
            )
        return (
            _BAND_RANK.get(row.match_band, 2),
            -(row.match_score if row.match_score is not None else 50),
            _VERDICT_RANK.get(row.evaluation_verdict, 2),
            _ELIGIBILITY_RANK.get(row.eligibility, 4),
            row.company.casefold(),
            row.title.casefold(),
            row.apply_url,
        )

    return sorted(
        rows,
        key=key,
    )


def rank_scout_rows(rows: list[FinalRow]) -> list[FinalRow]:
    """Order a saved shortlist the way the agent judged it.

    The agent's verified finds come first (tier 0), then any other row. Within a tier every ``keep`` comes before
    every ``unsure``, then the agent's priority (none reads as 2), then the order the agent sent the rows in, best
    first. ULTRON's own score never reorders what the agent ranked.
    """
    position = {id(row): index for index, row in enumerate(rows)}
    return sorted(rows, key=lambda row: (
        _tier(row),
        _VERDICT_RANK.get(row.evaluation_verdict, 2),
        row.priority if row.priority is not None else 2,
        position[id(row)],
    ))
