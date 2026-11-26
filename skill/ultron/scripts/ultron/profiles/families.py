"""Shared role-family matching for the watch and its ledger view."""

from __future__ import annotations

from pathlib import Path

import yaml

from ultron.profiles import store

Family = tuple[str, set[str]]


def role_family(profile: dict) -> Family:
    """Return the normalized role family label and vocabulary packs."""
    criteria = profile.get("criteria") if isinstance(profile, dict) else None
    if not isinstance(criteria, dict):
        return "", set()
    label = " ".join(str(criteria.get("role_family_label") or "").casefold().split())
    packs = criteria.get("vocabulary_packs")
    return label, {item.casefold() for item in packs if isinstance(item, str) and item} if isinstance(packs, list) else set()


def load_profile_families(user_dir: Path | None = None) -> dict[str, Family]:
    """Read valid profile families from disk without loading run state."""
    families = {}
    kwargs = {"user_dir": user_dir} if user_dir is not None else {}
    try:
        items = store.list_profiles(**kwargs)
    except (OSError, ValueError, TypeError):
        return families
    for item in items:
        slug = item["slug"]
        if not slug:
            continue
        try:
            profile = store.load_profile(slug, **kwargs)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        families[slug] = role_family(profile)
    return families


def related_profiles(profile_slug: str, family: Family, families: dict[str, Family]) -> list[str]:
    """Return other profiles with the same label or a shared vocabulary pack."""
    label, packs = family
    return sorted(slug for slug, (other_label, other_packs) in families.items() if slug != profile_slug
                  and ((label and label == other_label) or packs & other_packs))
