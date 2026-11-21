"""User documentation and repository hygiene checks."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "tools") not in sys.path:
    sys.path.insert(0, str(ROOT / "tools"))

import sync_wiki

README = ROOT / "README.md"
GUIDE_DIR = ROOT / "docs" / "guide"
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


def test_readme_describes_presets_unattended_and_links_user_guide():
    text = README.read_text(encoding="utf-8")
    for needle in (
        "docs/guide/README.md",
        "https://github.com/ERamaM/ULTRON/wiki",
        "Ambiguity Threshold",
        "New Companies Target",
        "zero AI tokens",
        "docs/guide/search-and-presets.md",
        "docs/guide/unattended-and-scheduling.md",
    ):
        assert needle in text, f"README.md missing {needle!r}"

    # Verify every relative Markdown link in README.md resolves inside the repo.
    in_fence = False
    for line in text.splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        for match in sync_wiki._MD_LINK_RE.finditer(line):
            target = match.group(2)
            if not target or target.startswith("#") or target.lower().startswith(("http://", "https://")):
                continue
            rel_path = target.split("#", 1)[0]
            assert (ROOT / rel_path).exists(), f"Broken link in README.md: {target!r}"


def test_user_guide_completeness_and_sync_wiki(tmp_path: Path, capsys):
    checked = sync_wiki.validate_guide_directory()
    assert len(checked) == 8

    out_dir = tmp_path / "wiki"
    (out_dir / ".git").mkdir(parents=True)
    stale = out_dir / "Old-Obsolete-Page.md"
    stale.write_text("stale", encoding="utf-8")
    assert sync_wiki.main(["--output", str(out_dir), "--clean"]) == 0
    assert "Synced 9 wiki files" in capsys.readouterr().out
    assert not stale.exists()
    assert (out_dir / ".git").is_dir()

    home_text = (out_dir / "Home.md").read_text(encoding="utf-8")
    assert "[Getting Started](Getting-Started)" in home_text
    assert "(getting-started.md)" not in home_text
    assert f"({sync_wiki.DEFAULT_BLOB_BASE}/README.md)" in home_text

    presets_text = (out_dir / "Search-and-Presets.md").read_text(encoding="utf-8")
    assert "[Unattended & Scheduling](Unattended-and-Scheduling)" in presets_text
    assert "`balanced`" in presets_text

    sidebar = (out_dir / "_Sidebar.md").read_text(encoding="utf-8")
    for wiki_file, title in sync_wiki.PAGE_MAP.values():
        assert f"[{title}]({wiki_file[:-3]})" in sidebar
        assert (out_dir / wiki_file).is_file()

    assert sync_wiki.main(["--check"]) == 0
    assert "Validated 8 guide pages" in capsys.readouterr().out


def test_sync_wiki_transform_and_error_paths(tmp_path: Path):
    sample = (
        "Intro [Guide](getting-started.md#install) and `[Skip](ignored.md)` and [External](https://example.com) "
        "and [Anchor](#local).\n"
        "```markdown\n[Fenced](also-ignored.md)\n```\n"
        "~~~sh\n[Tilde](ignored-too.md)\n~~~\n"
        "Tail [Root](../../README.md).\n"
    )
    rendered = sync_wiki.transform_markdown(sample)
    assert "[Guide](Getting-Started#install)" in rendered
    assert "`[Skip](ignored.md)`" in rendered
    assert "[Fenced](also-ignored.md)" in rendered
    assert "[Tilde](ignored-too.md)" in rendered
    assert f"[Root]({sync_wiki.DEFAULT_BLOB_BASE}/README.md)" in rendered

    # Test validation failures via synthetic repo/guide tree
    fake_repo = tmp_path / "repo"
    fake_guide = fake_repo / "docs" / "guide"
    fake_guide.mkdir(parents=True)
    with pytest.raises(ValueError, match="escapes repository"):
        sync_wiki.transform_markdown("[Escape](../../../etc/passwd)", repo_root=fake_repo)
    with pytest.raises(FileNotFoundError, match="broken relative link"):
        sync_wiki.transform_markdown("[Missing](nonexistent.md)", repo_root=fake_repo)
    with pytest.raises(KeyError, match="unmapped page"):
        (fake_guide / "extra.md").write_text("# Extra", encoding="utf-8")
        sync_wiki.transform_markdown("[Extra](extra.md)", repo_root=fake_repo)
    with pytest.raises(ValueError, match="unmapped guide files"):
        sync_wiki.validate_guide_directory(guide_dir=fake_guide, repo_root=fake_repo)
    (fake_guide / "extra.md").unlink()
    with pytest.raises(FileNotFoundError, match="missing expected guide files"):
        sync_wiki.validate_guide_directory(guide_dir=fake_guide, repo_root=fake_repo)
    with pytest.raises(FileNotFoundError, match="guide directory not found"):
        sync_wiki.validate_guide_directory(guide_dir=tmp_path / "missing-dir", repo_root=fake_repo)
    with pytest.raises(SystemExit):
        sync_wiki.main([])
    # Cover importlib check import cleanly
    assert importlib.util.find_spec("sync_wiki") is not None
