"""User documentation and repository hygiene checks."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
SKILL = ROOT / "skill" / "ultron"
BACKRONYM = "Universal Locator & Triage of Roles, Openings and Niches"


def _install_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    match = re.search(r"<!-- install:start -->\s*```[a-z]*\n(.*?)```\s*<!-- install:end -->", text, re.DOTALL)
    assert match, f"{path.name} needs an install block between install:start/end markers"
    return match.group(1).strip()


def test_readme_has_install_block():
    block = _install_block(README)
    assert "python install.py" in block


def test_backronym_present():
    assert BACKRONYM in README.read_text(encoding="utf-8")


def test_license_names_ultron_and_keeps_holder():
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License") and "Efrén Rama" in text


def test_readme_is_bounded():
    assert len(README.read_text(encoding="utf-8").splitlines()) <= 300


def test_readme_covers_bootstraps_windows_paths_extras_example_and_feeds():
    required = (
        "install.sh",
        "install.ps1",
        r'& "$HOME\.ultron\venv\Scripts\ultron.exe" doctor',
        r'"%USERPROFILE%\.ultron\venv\Scripts\ultron.exe" doctor',
        "~/.ultron/venv/bin/ultron doctor",
        "cv",
        "xlsx",
        "ultron profile install-example",
        "ultron run --unattended --profile ai-ml-remote-eu",
        "skill/ultron/data/feeds.yaml",
        "discover feeds",
    )
    text = README.read_text(encoding="utf-8")
    for needle in required:
        assert needle in text, f"README.md missing {needle!r}"


def test_production_repo_files_exist():
    for rel in ("install.sh", "install.ps1", "CONTRIBUTING.md", "SECURITY.md", ".gitleaks.toml"):
        assert (ROOT / rel).is_file(), f"Missing required repository file: {rel}"
    assert not (ROOT / "README.es.md").exists(), "README.es.md should not remain in English-only production repo"


def test_skill_and_references_contain_no_spanish_prompt_leftovers():
    docs = [SKILL / "SKILL.md", *sorted((SKILL / "references").glob("*.md"))]
    banned = ("busca ofertas de empleo", "ofertas nuevas", "busca roles de", "aplicada", "descartar")
    for path in docs:
        text = path.read_text(encoding="utf-8").lower()
        for needle in banned:
            assert needle not in text, f"{path.name} contains non-English phrase {needle!r}"
