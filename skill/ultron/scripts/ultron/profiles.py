"""Search profiles: user profiles in ``$ULTRON_HOME/profiles`` first, then bundled ones in ``data/profiles``."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import yaml

from ultron import home
from ultron.core.vocabulary import is_known_pack

SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*", re.ASCII)  # always used with fullmatch
EXAMPLE_SLUG = "ai-ml-remote-eu"
HISTORY_LIMIT = 20  # versions kept per profile in ``profiles/.history/<slug>/``
_VERSION_RE = re.compile(r"(\d{6})-(\d{8}T\d{6}Z)-([a-z]+)\.(yaml|absent)", re.ASCII)


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
    content = source.read_bytes()
    if target.exists():
        if target.read_bytes() == content:
            return target
        if not force:
            raise ProfileExistsError(f"{target} exists and has your changes; pass --force to overwrite it")
    _snapshot(EXAMPLE_SLUG, "install")
    _atomic_write_bytes(target, content)
    return target


class ProfileExistsError(FileExistsError):
    """Raised when saving over an existing user profile without ``force``."""


class ProfileInvalidError(ValueError):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def user_path(slug: str) -> Path:
    """Where the user's copy of ``slug`` lives (it may not exist yet)."""
    return _user_dir() / f"{slug}.yaml"


def profile_origin(slug: str) -> tuple[str, Path] | None:
    """``("user" | "bundled", path)`` of the file that ``load_profile(slug)`` reads, or ``None``."""
    path = _find(slug)
    if path is None:
        return None
    return ("user" if path.parent == _user_dir() else "bundled"), path


def dump_profile(data: dict) -> str:
    """The YAML text ULTRON writes for a profile (key order kept, Unicode as is)."""
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)


def save_profile(data: dict, *, force: bool = False) -> Path:
    """Validate ``data`` and write it to ``$ULTRON_HOME/profiles/<slug>.yaml`` (no overwrite without ``force``)."""
    errors = validate_profile(data)
    if errors:
        raise ProfileInvalidError(errors)
    if user_path(data["slug"]).exists() and not force:
        raise ProfileExistsError(f"profile already exists: {data['slug']} (use --force to overwrite)")
    return write_profile(data, reason="save")[0]


def write_profile(data: dict, *, reason: str) -> tuple[Path, str]:
    """Validate and atomically write ``data`` as the user's profile, keeping the previous version in history.

    Returns ``(path, version)``; ``version`` is the history entry that undoes this write.
    """
    errors = validate_profile(data)
    if errors:
        raise ProfileInvalidError(errors)
    version = _snapshot(data["slug"], reason)
    target = user_path(data["slug"])
    _atomic_write_bytes(target, dump_profile(data).encode("utf-8"))
    return target, version


# --- version history ---------------------------------------------------------------------------------
#
# Before every write, the user's current file is copied byte for byte (comments included) to
# ``profiles/.history/<slug>/<seq>-<UTC time>-<reason>.yaml``. When there was no user file, an empty
# ``.absent`` marker records that, so undoing the write removes the user copy again (a bundled profile
# with the same slug then applies). ``list_profiles`` only reads ``profiles/*.yaml``, so the hidden
# folder never shows up as a profile, and data maintenance never touches ``profiles/``.


class NoHistoryError(LookupError):
    """Raised when a profile has no earlier version to restore."""


class VersionNotFoundError(LookupError):
    """Raised when ``undo --to`` names a version that is not in the profile's history."""


def _history_dir(slug: str) -> Path:
    return _user_dir() / ".history" / slug


def _versions(slug: str) -> list[tuple[int, str, str, Path]]:
    """``(seq, saved_at, reason, path)`` for every history entry, oldest first."""
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        return []
    folder = _history_dir(slug)
    if not folder.is_dir():
        return []
    found = []
    for path in folder.iterdir():
        match = _VERSION_RE.fullmatch(path.name)
        if match and path.is_file():
            found.append((int(match.group(1)), match.group(2), match.group(3), path))
    return sorted(found)


def _version_id(path: Path) -> str:
    return path.name.rsplit(".", 1)[0]


def _snapshot(slug: str, reason: str) -> str:
    """Record the user's current file (or its absence) before a write; return the version id."""
    if not SLUG_RE.fullmatch(slug):
        raise ValueError(f"invalid profile slug: {slug!r}")
    existing = _versions(slug)
    seq = existing[-1][0] + 1 if existing else 1
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    current = user_path(slug)
    if current.is_file():
        target = _history_dir(slug) / f"{seq:06d}-{stamp}-{reason}.yaml"
        _atomic_write_bytes(target, current.read_bytes())
    else:
        target = _history_dir(slug) / f"{seq:06d}-{stamp}-{reason}.absent"
        _atomic_write_bytes(target, b"")
    for *_rest, old in _versions(slug)[:-HISTORY_LIMIT]:
        old.unlink(missing_ok=True)
    return _version_id(target)


def history(slug: str) -> list[dict]:
    """The saved versions of ``slug``, newest first (``exists`` is false for a "no user copy" marker)."""
    return [
        {"version": _version_id(path), "saved_at": saved_at, "reason": reason, "exists": path.suffix == ".yaml"}
        for _seq, saved_at, reason, path in reversed(_versions(slug))
    ]


def _undo_target(slug: str, version: str | None) -> Path:
    versions = _versions(slug)
    if version is not None:
        for *_rest, path in versions:
            if _version_id(path) == version:
                return path
        raise VersionNotFoundError(f"version not found for {slug}: {version}")
    # A plain undo steps back through the changes. Versions written by an undo itself are skipped, so
    # repeated undos keep going back instead of toggling; restore one of them with ``--to`` to redo.
    for _seq, _saved_at, reason, path in reversed(versions):
        if reason != "undo":
            return path
    raise NoHistoryError(f"no earlier version of {slug} to restore")


def version_content(slug: str, version: str | None = None) -> tuple[str, str | None]:
    """``(version id, YAML text, or None when that version had no user copy)`` that ``restore`` would apply."""
    path = _undo_target(slug, version)
    return _version_id(path), (path.read_text(encoding="utf-8") if path.suffix == ".yaml" else None)


def restore(slug: str, version: str | None = None) -> dict:
    """Restore a version (default: the one before the last change); the replaced file is kept as ``undo``.

    A plain undo consumes the version it restores; an explicit ``version`` stays in the history.
    """
    path = _undo_target(slug, version)
    # Read the version before snapshotting: the snapshot may prune the oldest entry, which can be this one.
    content = path.read_bytes() if path.suffix == ".yaml" else None
    backup = _snapshot(slug, "undo")
    target = user_path(slug)
    if content is not None:
        _atomic_write_bytes(target, content)
    else:
        target.unlink(missing_ok=True)
    if version is None:
        path.unlink(missing_ok=True)
    return {"restored": _version_id(path), "backup": backup, "removed_user_copy": content is None}


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
