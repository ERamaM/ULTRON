"""Search profiles: user profiles in ``$ULTRON_HOME/profiles`` first, then bundled ones in ``data/profiles``."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from ultron import home
from ultron.core.vocabulary import is_known_pack

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*", re.ASCII)  # always used with fullmatch
EXAMPLE_SLUG = "ai-ml-remote-eu"


def _user_dir() -> Path:
    return home.ultron_home() / "profiles"


def _bundled_dir() -> Path:
    return home.data_dir() / "profiles"


def _find(slug: str) -> Path | None:
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        return None
    for folder in (_user_dir(), _bundled_dir()):
        path = folder / f"{slug}.yaml"
        if path.is_file():
            return path
    return None


def load_profile(slug: str) -> dict:
    path = _find(slug)
    if path is None:
        raise FileNotFoundError(f"profile not found: {slug}")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def list_profiles() -> list[dict]:
    found: dict[str, dict] = {}
    for folder in (_bundled_dir(), _user_dir()):  # user entries override bundled ones
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                data = {}
            if not isinstance(data, dict):
                data = {}
            slug = data.get("slug") or path.stem
            entry = {"slug": slug, "name": data.get("name", ""), "path": str(path)}
            if data.get("source_cv"):
                entry["source_cv"] = data["source_cv"]
            found[slug] = entry
    return sorted(found.values(), key=lambda p: p["slug"])


def _term_list_errors(value, where: str) -> list[str]:
    errors: list[str] = []
    if isinstance(value, list):
        if not all(isinstance(item, str) for item in value):
            errors.append(f"{where} must be a list of strings")
    elif isinstance(value, dict):
        for key, inner in value.items():
            errors.extend(_term_list_errors(inner, f"{where}.{key}"))
    return errors


def validate_profile(data: dict) -> list[str]:
    if not isinstance(data, dict):
        return ["profile must be a mapping"]
    errors: list[str] = []
    slug = data.get("slug")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        errors.append("slug must match ^[a-z0-9][a-z0-9-]*$")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        errors.append("name is required")
    if data.get("source_cv") is not None and not isinstance(data["source_cv"], str):
        errors.append("source_cv must be a string (path of the CV) or null")
    criteria = data.get("criteria")
    if not isinstance(criteria, dict):
        errors.append("criteria must be a mapping")
        return errors
    if not (criteria.get("target_titles") or criteria.get("role_focus_terms")):
        errors.append("criteria needs a non-empty target_titles or role_focus_terms")
    policy = criteria.get("remote_policy")
    if isinstance(policy, dict) and "required" in policy and policy["required"] not in {"full_remote", "any"}:
        errors.append("criteria.remote_policy.required must be 'full_remote' or 'any'")
    errors.extend(_term_list_errors(criteria, "criteria"))
    errors.extend(_vocabulary_pack_errors(criteria.get("vocabulary_packs")))
    return errors


def _vocabulary_pack_errors(value) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return ["criteria.vocabulary_packs must be a list of pack names"]
    return [f"criteria.vocabulary_packs: unknown pack {name!r}" for name in value if not is_known_pack(name)]


def install_example(*, force: bool = False) -> Path:
    """Copy the bundled example to the user folder; an existing, different user copy needs ``force``."""
    source = _bundled_dir() / f"{EXAMPLE_SLUG}.yaml"
    target = _user_dir() / source.name
    if target.exists() and not force:
        if target.read_bytes() == source.read_bytes():
            return target
        raise ProfileExistsError(f"{target} exists and has your changes; pass --force to overwrite it")
    shutil.copyfile(source, target)
    return target


class ProfileExistsError(FileExistsError):
    """Raised when saving over an existing user profile without ``force``."""


class ProfileInvalidError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def save_profile(data: dict, *, force: bool = False) -> Path:
    """Validate ``data`` and write it to ``$ULTRON_HOME/profiles/<slug>.yaml`` (no overwrite without ``force``)."""
    errors = validate_profile(data)
    if errors:
        raise ProfileInvalidError(errors)
    target = _user_dir() / f"{data['slug']}.yaml"
    if target.exists() and not force:
        raise ProfileExistsError(f"profile already exists: {data['slug']} (use --force to overwrite)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target
