"""Verify that a GitHub release tag matches both checked-in version declarations."""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "skill" / "ultron" / "scripts"


def _runtime_version(path: Path) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        ) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            return node.value.value
    raise ValueError("runtime version must be a string literal")


def main(argv: list[str] | None = None) -> int:
    """Fail before publication when the tag, package and runtime versions disagree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    tag = parser.parse_args(argv).tag
    with (PACKAGE / "pyproject.toml").open("rb") as config:
        package = tomllib.load(config)["project"]["version"]
    runtime = _runtime_version(PACKAGE / "ultron" / "__init__.py")
    if tag == f"v{package}" == f"v{runtime}":
        return 0
    print(f"Release tag {tag!r} disagrees with package version {package!r} or runtime version {runtime!r}.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
