"""Package layering rules from docs/architecture.md, checked on every import in the ultron package."""

from __future__ import annotations

import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "skill" / "ultron" / "scripts" / "ultron"
MAX_MODULE_LINES = 1000
# Over the limit, to be split next; never add to this list.
KNOWN_LARGE = {"ultron.core.extractors.ats_extractors.base", "ultron.cv.ats"}


def _modules() -> dict[str, Path]:
    found = {}
    for path in PACKAGE.rglob("*.py"):
        parts = path.relative_to(PACKAGE.parent).with_suffix("").parts
        name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
        found[name] = path
    return found


def _imports(path: Path) -> set[str]:
    """Every ``ultron`` module or attribute a file imports, as dotted names."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "ultron":
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names if alias.name.split(".")[0] == "ultron")
    return names


def _is_command(name: str) -> bool:
    return name in {"ultron.cli", "ultron.commands"} or name.startswith("ultron.commands.")


def _violations(owner: str, allowed: tuple[str, ...]) -> list[str]:
    bad = []
    for name, path in _modules().items():
        if name != owner and not name.startswith(owner + "."):
            continue
        for dep in _imports(path):
            if dep == "ultron" or dep == "ultron.__version__":
                continue
            if not any(dep == pkg or dep.startswith(pkg + ".") for pkg in allowed):
                bad.append(f"{name} imports {dep}")
    return sorted(bad)


def test_infra_depends_on_nothing_else_in_ultron():
    assert _violations("ultron.infra", ("ultron.infra",)) == []


def test_core_depends_only_on_core_and_infra():
    assert _violations("ultron.core", ("ultron.core", "ultron.infra")) == []


def test_only_commands_import_commands():
    bad = []
    for name, path in _modules().items():
        if _is_command(name):
            continue
        for dep in _imports(path):
            parts = dep.split(".")
            if any(_is_command(".".join(parts[:i])) for i in range(2, len(parts) + 1)):
                bad.append(f"{name} imports {dep}")
    assert sorted(bad) == []


def test_modules_stay_small():
    sizes = {name: len(path.read_text(encoding="utf-8").splitlines()) for name, path in _modules().items()}
    big = {name: n for name, n in sizes.items() if n > MAX_MODULE_LINES and name not in KNOWN_LARGE}
    assert big == {}, f"split these modules (over {MAX_MODULE_LINES} lines): {big}"
