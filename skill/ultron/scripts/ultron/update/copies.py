"""Installed ULTRON copies: global and project skill folders, install records and their grouping by scope."""

from __future__ import annotations

import json
import os
from pathlib import Path

from ultron.infra import installs as install_layout
from ultron.infra.platforms import PLATFORM_NAMES, global_skill_dirs

PROJECT_AGENTS_PLATFORMS = ("codex", "opencode", "antigravity")  # share DIR/.agents/skills/ultron


# --------------------------------------------------------------------------------------------- scopes


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (os.name == "nt" and path.is_junction())


def _is_copy_install(path: Path) -> bool:
    """A real folder with a valid installer marker; links (dev installs) never qualify."""
    return not _is_link(path) and path.is_dir() and install_layout.marker_valid(path)


def _ordered(platforms: set[str]) -> list[str]:
    return [name for name in PLATFORM_NAMES if name in platforms]


def _project_dirs(home: Path, cwd: Path, projects: list[Path]) -> list[Path]:
    """The current folder plus ``--project`` folders, resolved and deduplicated (the home is global scope)."""
    out: list[Path] = []
    for folder in [cwd, *projects]:
        resolved = Path(folder).expanduser().resolve()
        if resolved != Path(home).resolve() and resolved not in out:
            out.append(resolved)
    return out


def _project_skill_dirs(project: Path) -> dict[tuple[str, ...], Path]:
    return {
        ("claude",): project / ".claude" / "skills" / "ultron",
        PROJECT_AGENTS_PLATFORMS: project / ".agents" / "skills" / "ultron",
    }


def _real(path: Path) -> str:
    """The key ``install.py`` uses in ``installs.json`` (parents resolved, last component not followed)."""
    return str(Path(path).parent.resolve() / Path(path).name)


def _registry_records() -> dict:
    """``installs`` from ``$ULTRON_HOME/installs.json`` (written by install.py); empty for older/missing files."""
    try:
        data = json.loads((install_layout.home_path() / "installs.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    records = data.get("installs") if isinstance(data, dict) else None
    return records if isinstance(records, dict) else {}


def _marker_version(path: Path) -> str | None:
    try:
        version = json.loads((path / ".ultron-install.json").read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError, UnicodeDecodeError, AttributeError):
        return None
    return version if isinstance(version, str) else None


def _platforms(names: object) -> list[str]:
    if not isinstance(names, list):
        return []
    return _ordered({"antigravity" if n == "antigravity-legacy" else n for n in names if isinstance(n, str)})


def _recorded_copies() -> list[dict]:
    """Copy installs recorded by install.py that still exist and still carry a valid marker."""
    copies = []
    for record in _registry_records().values():
        if not isinstance(record, dict) or record.get("mode") == "link" or not isinstance(record.get("path"), str):
            continue
        scope, project, platforms = record.get("scope"), record.get("project"), _platforms(record.get("platforms"))
        if scope not in ("global", "project") or not platforms or (scope == "project" and not isinstance(project, str)):
            continue
        path = Path(record["path"])
        if not _is_copy_install(path):  # removed or replaced since it was recorded
            continue
        # The marker is rewritten with every copy; the record may lag if its write failed (only a warning).
        version = _marker_version(path) or (record.get("version") if isinstance(record.get("version"), str) else None)
        copies.append({"scope": scope, "project": project if scope == "project" else None, "path": str(path),
                       "platforms": platforms, "version": version, "found_by": "record"})
    return copies


def missing_records() -> list[str]:
    """Recorded install folders that no longer exist (deleted by hand); install.py drops them on its next write."""
    missing = []
    for record in _registry_records().values():
        path = record.get("path") if isinstance(record, dict) else None
        if isinstance(path, str) and _definitely_missing(path):
            missing.append(path)
    return missing


def _definitely_missing(path: str) -> bool:
    """Only "not found" counts; any other error (permissions, offline drive) keeps the record (see install.py)."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except (OSError, ValueError):
        return False
    return False


def find_copies(*, home: Path, cwd: Path, projects: list[Path]) -> list[dict]:
    """Every copy install to consider, one entry per skill folder.

    Records in ``installs.json`` come first (they find project installs anywhere). Installs made before
    install.py recorded them are found by their ``.ultron-install.json`` marker in the platforms' global folders,
    ``cwd`` and ``projects``.
    """
    copies = _recorded_copies()
    seen = {_real(Path(c["path"])) for c in copies}

    def add(scope: str, project: str | None, path: Path, platforms: list[str]) -> None:
        if _real(path) not in seen and _is_copy_install(path):
            seen.add(_real(path))
            copies.append({"scope": scope, "project": project, "path": str(path), "platforms": platforms,
                           "version": _marker_version(path), "found_by": "marker"})

    for name, path in global_skill_dirs(home).items():
        add("global", None, path, ["antigravity" if name == "antigravity-legacy" else name])
    for project in _project_dirs(home, cwd, projects):
        for platforms, path in _project_skill_dirs(project).items():
            add("project", str(project), path, list(platforms))
    return copies


def group_scopes(copies: list[dict]) -> list[dict]:
    """One entry per installer scope (global, or one project folder) with the union of platforms."""
    groups: dict[tuple[str, str | None], dict] = {}
    for copy in copies:
        key = (copy["scope"], copy["project"])
        group = groups.setdefault(key, {"scope": copy["scope"], "platforms": set(), "paths": [], "versions": {}})
        if copy["scope"] == "project":
            group["project"] = copy["project"]
        group["platforms"].update(copy["platforms"])
        group["paths"].append(copy["path"])
        group["versions"][copy["path"]] = copy["version"]
    return [{**group, "platforms": _ordered(group["platforms"])} for group in groups.values()]


def split_mixed_antigravity(groups: list[dict], home: Path) -> tuple[list[dict], list[dict]]:
    """Drop ``antigravity`` from the global command when one of its two folders is a registered dev link.

    ``install.py --global --platforms antigravity`` refreshes both the current and the legacy Antigravity folder, so
    it would replace that dev link with a copy. The copy is left for the user to update by hand instead.
    """
    dirs = global_skill_dirs(home)
    links = [str(p) for k, p in dirs.items() if k.startswith("antigravity") and _is_link(p) and install_layout.registered_link(p)]
    if not links:
        return groups, []
    out, manual = [], []
    for group in groups:
        if group["scope"] == "global" and "antigravity" in group["platforms"]:
            manual.append({
                "platform": "antigravity",
                "dev_links": links,
                "detail": "one Antigravity folder is a --dev link: the installer would replace it, so the "
                          "Antigravity copy is not updated here; update it by hand or remove one of the two",
            })
            group = {**group, "platforms": [p for p in group["platforms"] if p != "antigravity"]}
            if not group["platforms"]:
                continue
        out.append(group)
    return out, manual


def discover_installs(*, home: Path, cwd: Path, projects: list[Path]) -> list[dict]:
    """Installer scopes to refresh (see ``find_copies``)."""
    return group_scopes(find_copies(home=home, cwd=cwd, projects=projects))


def dev_links(*, home: Path, cwd: Path, projects: list[Path]) -> list[str]:
    """Linked skill folders registered in ``installs.json`` (``--dev`` installs): updated with git, not here."""
    candidates = list(global_skill_dirs(home).values())
    for project in _project_dirs(home, cwd, projects):
        candidates.extend(_project_skill_dirs(project).values())
    for record in _registry_records().values():
        if isinstance(record, dict) and record.get("mode") == "link" and isinstance(record.get("path"), str):
            candidates.append(Path(record["path"]))
    out: list[str] = []
    for path in candidates:
        if _is_link(path) and install_layout.registered_link(path) and str(path) not in out:
            out.append(str(path))
    return out


