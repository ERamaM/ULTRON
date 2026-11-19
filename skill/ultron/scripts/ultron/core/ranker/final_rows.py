from __future__ import annotations

import re
import unicodedata

from ultron.core.models import COMPANY_NOT_FOUND, LOCATION_NOT_SHOWN, TITLE_NOT_FOUND, FinalRow

_TITLE_NOISE_RE = re.compile(r"\((?:[^)]*)\)|\[(?:[^\]]*)\]")
_TITLE_MODE_RE = re.compile(r"\b(remote|remoto|hybrid|hibrido|teletrabajo)\b")
_VERDICT_RANK = {"keep": 0, "unsure": 1}
_ELIGIBILITY_RANK = {"confirmed": 0, "high": 1, "medium": 2, "unknown": 3}


def normalize_title_for_dedup(title: str) -> str:
    """Collapse title variants that only differ by mode/noise/accents."""
    folded = unicodedata.normalize("NFKD", str(title or ""))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    low = _TITLE_NOISE_RE.sub(" ", folded.lower())
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
        return self._count


def _duplicate_quality(row: FinalRow) -> tuple[int, int, float, bool]:
    return (
        _VERDICT_RANK.get(row.evaluation_verdict, 2),
        _ELIGIBILITY_RANK.get(row.eligibility, 4),
        -row.match_score if row.match_score is not None else float("inf"),
        str(row.location or "").strip().lower() in {"", LOCATION_NOT_SHOWN},
    )


def dedupe_final_rows(rows: list[FinalRow]) -> tuple[list[FinalRow], list[FinalRow]]:
    """Keep each group's best row in its first arrival's position."""
    kept: list[FinalRow] = []
    duplicates: list[FinalRow] = []
    identities = RoleIdentityIndex()
    for row in rows:
        group = identities.group_key(row)
        if group is None:
            identities.add(row)
            kept.append(row)
        elif _duplicate_quality(row) < _duplicate_quality(kept[int(group)]):
            duplicates.append(kept[int(group)])
            kept[int(group)] = row
        else:
            duplicates.append(row)
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
