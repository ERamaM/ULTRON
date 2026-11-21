"""Sync versioned user-guide Markdown files in ``docs/guide/`` to GitHub Wiki format."""

from __future__ import annotations

import argparse
import posixpath
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE_DIR = ROOT / "docs" / "guide"
DEFAULT_BLOB_BASE = "https://github.com/ERamaM/ULTRON/blob/main"
REPO_HOME_URL = "https://github.com/ERamaM/ULTRON#readme"

PAGE_MAP: dict[str, tuple[str, str]] = {
    "README.md": ("Home.md", "Home"),
    "getting-started.md": ("Getting-Started.md", "Getting Started"),
    "search-profiles.md": ("Search-Profiles.md", "Search Profiles"),
    "search-and-presets.md": ("Search-and-Presets.md", "Searches & Presets"),
    "unattended-and-scheduling.md": ("Unattended-and-Scheduling.md", "Unattended & Scheduling"),
    "tracking-and-exports.md": ("Tracking-and-Exports.md", "Tracking & Exports"),
    "career-tools.md": ("Career-Tools.md", "Career Tools"),
    "troubleshooting-and-data.md": ("Troubleshooting-and-Data.md", "Troubleshooting & Data"),
}

_MD_LINK_RE = re.compile(r"\[([^\]\n]{1,300})\]\(([^()\s\n]{1,400})\)")
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:", "tel:")


def _split_inline_code(line: str) -> list[tuple[str, bool]]:
    """Split ``line`` into ``(segment, is_code)`` pairs preserving backtick spans verbatim."""
    parts: list[tuple[str, bool]] = []
    pos = 0
    length = len(line)
    while pos < length:
        tick_start = line.find("`", pos)
        if tick_start == -1:
            parts.append((line[pos:], False))
            break
        if tick_start > pos:
            parts.append((line[pos:tick_start], False))
        tick_end = line.find("`", tick_start + 1)
        if tick_end == -1:
            parts.append((line[tick_start:], False))
            break
        parts.append((line[tick_start : tick_end + 1], True))
        pos = tick_end + 1
    return parts


def _rewrite_target(
    raw_target: str,
    *,
    repo_root: Path,
    blob_base: str,
    validate_targets: bool,
) -> str:
    if not raw_target or raw_target.startswith("#") or raw_target.lower().startswith(_EXTERNAL_PREFIXES):
        return raw_target

    hash_idx = raw_target.find("#")
    raw_path = raw_target[:hash_idx] if hash_idx != -1 else raw_target
    anchor = raw_target[hash_idx:] if hash_idx != -1 else ""
    if not raw_path:
        return raw_target

    normalized = posixpath.normpath(f"docs/guide/{raw_path}")
    if normalized == ".." or normalized.startswith("../"):
        raise ValueError(f"guide link escapes repository root: {raw_target!r}")

    if validate_targets and not (repo_root / normalized).exists():
        raise FileNotFoundError(f"broken relative link {raw_target!r} -> {normalized!r}")

    if normalized.startswith("docs/guide/"):
        guide_name = normalized[len("docs/guide/") :]
        if guide_name not in PAGE_MAP:
            raise KeyError(f"guide link references unmapped page {guide_name!r} (target {raw_target!r})")
        wiki_filename, _title = PAGE_MAP[guide_name]
        return f"{wiki_filename[:-3]}{anchor}"

    return f"{blob_base.rstrip('/')}/{normalized}{anchor}"


def transform_markdown(
    text: str,
    *,
    repo_root: Path = ROOT,
    blob_base: str = DEFAULT_BLOB_BASE,
    validate_targets: bool = True,
) -> str:
    """Convert a ``docs/guide/*.md`` document into GitHub Wiki Markdown without modifying code spans."""
    out_lines: list[str] = []
    in_fence = False
    fence_char = ""

    def _replace_match(match: re.Match[str]) -> str:
        label, target = match.group(1), match.group(2)
        rewritten = _rewrite_target(
            target,
            repo_root=repo_root,
            blob_base=blob_base,
            validate_targets=validate_targets,
        )
        return f"[{label}]({rewritten})"

    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_fence and stripped.startswith(("```", "~~~")):
            in_fence = True
            fence_char = stripped[:3]
            out_lines.append(line)
            continue
        if in_fence:
            if stripped.startswith(fence_char):
                in_fence = False
                fence_char = ""
            out_lines.append(line)
            continue

        rebuilt: list[str] = []
        for segment, is_code in _split_inline_code(line):
            if is_code:
                rebuilt.append(segment)
            else:
                rebuilt.append(_MD_LINK_RE.sub(_replace_match, segment))
        out_lines.append("".join(rebuilt))

    return "".join(out_lines)


def build_sidebar() -> str:
    """Render ``_Sidebar.md`` navigation for the GitHub Wiki."""
    lines = ["### ULTRON User Guide", ""]
    for wiki_file, title in PAGE_MAP.values():
        lines.append(f"- [{title}]({wiki_file[:-3]})")
    lines.extend(["", "---", "", f"- [Repository README]({REPO_HOME_URL})", ""])
    return "\n".join(lines)


def validate_guide_directory(
    *,
    guide_dir: Path = GUIDE_DIR,
    repo_root: Path = ROOT,
    blob_base: str = DEFAULT_BLOB_BASE,
) -> list[Path]:
    """Verify every file in ``guide_dir`` is mapped and every relative Markdown link resolves."""
    if not guide_dir.is_dir():
        raise FileNotFoundError(f"guide directory not found: {guide_dir}")

    disk_files = {p.name for p in guide_dir.iterdir() if p.is_file() and p.suffix == ".md"}
    expected_files = set(PAGE_MAP)
    unmapped = sorted(disk_files - expected_files)
    if unmapped:
        raise ValueError(f"unmapped guide files in {guide_dir}: {', '.join(unmapped)}")
    missing = sorted(expected_files - disk_files)
    if missing:
        raise FileNotFoundError(f"missing expected guide files in {guide_dir}: {', '.join(missing)}")

    checked: list[Path] = []
    for name in PAGE_MAP:
        path = guide_dir / name
        transform_markdown(
            path.read_text(encoding="utf-8"),
            repo_root=repo_root,
            blob_base=blob_base,
            validate_targets=True,
        )
        checked.append(path)
    return checked


def sync_wiki(
    output_dir: Path,
    *,
    guide_dir: Path = GUIDE_DIR,
    repo_root: Path = ROOT,
    blob_base: str = DEFAULT_BLOB_BASE,
    clean: bool = False,
) -> list[Path]:
    """Render all guide pages and ``_Sidebar.md`` into ``output_dir``."""
    validate_guide_directory(guide_dir=guide_dir, repo_root=repo_root, blob_base=blob_base)
    dest = Path(output_dir)
    dest.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    expected_names = {"_Sidebar.md"}
    for src_name, (wiki_name, _title) in PAGE_MAP.items():
        raw = (guide_dir / src_name).read_text(encoding="utf-8")
        rendered = transform_markdown(raw, repo_root=repo_root, blob_base=blob_base, validate_targets=True)
        out_path = dest / wiki_name
        out_path.write_text(rendered, encoding="utf-8")
        expected_names.add(wiki_name)
        written.append(out_path)

    sidebar_path = dest / "_Sidebar.md"
    sidebar_path.write_text(build_sidebar(), encoding="utf-8")
    written.append(sidebar_path)

    if clean:
        for entry in dest.iterdir():
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md" and entry.name not in expected_names:
                entry.unlink()

    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or export docs/guide/ to GitHub Wiki format.")
    parser.add_argument("--check", action="store_true", help="validate guide files and relative links without writing")
    parser.add_argument("--output", help="target directory (e.g. cloned .wiki.git checkout) for exported Wiki pages")
    parser.add_argument("--clean", action="store_true", help="remove stale top-level .md files in --output")
    parser.add_argument("--blob-base", default=DEFAULT_BLOB_BASE, help="GitHub blob URL prefix for non-guide links")
    args = parser.parse_args(argv)

    if not args.check and not args.output:
        parser.error("pass --check or --output DIR")

    validate_guide_directory(blob_base=args.blob_base)
    if args.output:
        written = sync_wiki(Path(args.output), clean=args.clean, blob_base=args.blob_base)
        print(f"Synced {len(written)} wiki files to {args.output}")
    else:
        print(f"Validated {len(PAGE_MAP)} guide pages and links")
    return 0


if __name__ == "__main__":
    sys.exit(main())
