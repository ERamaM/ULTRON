"""Opt-in vocabulary packs: role-family terms as data, not core code.

A pack lives in ``data/vocabulary/<name>.yaml`` and is enabled per profile with
``criteria.vocabulary_packs: [<name>, ...]``. With no pack, the core only uses the
profile's own terms (no family default, no "family-aware" discovery heuristics).

The first eight fields are the brief's interface; ``title_synonym_groups`` and
``title_anchor_terms`` were added because two more family-specific constants lived
in core code (title synonym expansion and the "title names the family" check).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from functools import lru_cache

import yaml

from ultron.infra.home import data_dir

PACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class VocabularyPack:
    role_terms: tuple[str, ...] = ()
    title_terms: tuple[str, ...] = ()
    extra_title_terms: tuple[str, ...] = ()
    coarse_signal_wb: tuple[str, ...] = ()
    coarse_signal_substr: tuple[str, ...] = ()
    ambiguity_terms: tuple[str, ...] = ()
    ambiguity_text_signals: tuple[str, ...] = ()
    non_family_signals: tuple[str, ...] = ()
    title_synonym_groups: tuple[tuple[str, ...], ...] = ()
    title_anchor_terms: tuple[str, ...] = ()


EMPTY_PACK = VocabularyPack()
_FIELDS = tuple(field.name for field in fields(VocabularyPack))
_GROUP_FIELDS = {"title_synonym_groups"}


def vocabulary_dir():
    return data_dir() / "vocabulary"


def is_known_pack(name: object) -> bool:
    return isinstance(name, str) and bool(PACK_NAME_RE.match(name)) and (vocabulary_dir() / f"{name}.yaml").is_file()


def _strings(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{where} must be a list of strings")
    return tuple(value)


@lru_cache(maxsize=32)
def _load_one(name: str) -> VocabularyPack:
    if not is_known_pack(name):
        raise ValueError(f"unknown vocabulary pack: {name!r}")
    path = vocabulary_dir() / f"{name}.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise TypeError(f"vocabulary pack {name!r} must be a mapping")
    unknown = sorted(set(raw) - set(_FIELDS))
    if unknown:
        raise ValueError(f"vocabulary pack {name!r} has unknown keys: {', '.join(unknown)}")
    values: dict[str, tuple] = {}
    for key, value in raw.items():
        if key in _GROUP_FIELDS:
            if not isinstance(value, list):
                raise ValueError(f"{name}.{key} must be a list of lists of strings")
            values[key] = tuple(_strings(group, f"{name}.{key}[]") for group in value)
        else:
            values[key] = _strings(value, f"{name}.{key}")
    return VocabularyPack(**values)


def load_packs(names: Sequence[str]) -> VocabularyPack:
    """Merge the named packs (order-preserving, de-duplicated); empty pack for no names."""
    packs = [_load_one(str(name)) for name in names]
    if not packs:
        return EMPTY_PACK
    if len(packs) == 1:
        return packs[0]
    merged = {key: tuple(dict.fromkeys(item for pack in packs for item in getattr(pack, key))) for key in _FIELDS}
    return VocabularyPack(**merged)


def active_pack(profile: Mapping[str, object] | None) -> VocabularyPack:
    """The pack selected by ``profile["criteria"]["vocabulary_packs"]`` (empty when absent)."""
    criteria = (profile or {}).get("criteria")
    names = criteria.get("vocabulary_packs") if isinstance(criteria, Mapping) else None
    if not names:
        return EMPTY_PACK
    if isinstance(names, str) or not isinstance(names, Sequence):
        raise TypeError("criteria.vocabulary_packs must be a list of pack names")
    return load_packs(tuple(names))
