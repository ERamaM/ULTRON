"""Platform table: where each agent platform looks for global skills and how to recognise it from the environment.

Single source of truth for ``ultron doctor``. ``install.py`` (a repo script, not part of the package) keeps its own
copy of the global paths in ``platform_targets``; it could later share this table by loading this file with importlib.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

PLATFORM_NAMES = ("claude", "codex", "opencode", "antigravity")

# Section titles in ``references/platforms.md``.
PLATFORM_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
    "antigravity": "Antigravity",
}

# Global skill folder relative to the user's home (spec section 3).
GLOBAL_SKILL_PATHS = {
    "claude": (".claude", "skills", "ultron"),
    "codex": (".agents", "skills", "ultron"),
    "opencode": (".config", "opencode", "skills", "ultron"),
    "antigravity": (".gemini", "config", "skills", "ultron"),
}
# Extra location checked only when ``~/.gemini/antigravity`` exists.
ANTIGRAVITY_LEGACY = (".gemini", "antigravity", "skills", "ultron")

# Environment variables that identify the platform running the agent.
_ENV_NAMES = {
    "claude": ("CLAUDECODE",),
    "codex": ("CODEX_SANDBOX", "CODEX_HOME"),
    "opencode": ("OPENCODE",),
    "antigravity": (),
}
_ENV_PREFIXES = {"antigravity": ("ANTIGRAVITY",)}


def global_skill_dirs(home: Path) -> dict[str, Path]:
    """Platform -> global skill folder; the legacy Antigravity folder appears as ``antigravity-legacy``."""
    out = {name: Path(home).joinpath(*parts) for name, parts in GLOBAL_SKILL_PATHS.items()}
    if (Path(home) / ".gemini" / "antigravity").is_dir():
        out["antigravity-legacy"] = Path(home).joinpath(*ANTIGRAVITY_LEGACY)
    return out


def detect_platforms(env: Mapping[str, str]) -> list[str]:
    """Platforms whose marker variables are set (in ``PLATFORM_NAMES`` order); empty when unknown."""
    found = []
    for name in PLATFORM_NAMES:
        names = _ENV_NAMES.get(name, ())
        prefixes = _ENV_PREFIXES.get(name, ())
        if any(env.get(n) for n in names) or any(k.startswith(prefixes) and env[k] for k in env if prefixes):
            found.append(name)
    return found


def project_skill_dirs(cwd: Path) -> dict[str, Path]:
    """Project-scope skill folders (``install.py --project DIR``): Claude Code and the shared ``.agents`` folder."""
    return {
        "project .claude": Path(cwd) / ".claude" / "skills" / "ultron",
        "project .agents": Path(cwd) / ".agents" / "skills" / "ultron",
    }
