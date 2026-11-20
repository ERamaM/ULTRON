# Contributing to ULTRON

Contributions are welcome. ULTRON aims to keep its skill instructions concise, its heuristics deterministic, and its test suite fast across Linux, macOS, and Windows.

## Reporting an Issue

Use the [GitHub issue forms](https://github.com/ERamaM/ULTRON/issues/new/choose) for bugs, usage questions, and feature requests. For a bug, use a minimal synthetic example and include `ultron doctor --public` if the CLI starts. The skill can prepare and submit a reviewed report with `system report`. Use the [private reporting channel](SECURITY.md#reporting-a-vulnerability) for security vulnerabilities.

## Development Setup

1. Ensure [uv](https://docs.astral.sh/uv/) and Git are installed (`python >= 3.12` is managed automatically by `uv`).
2. Install workspace and dev dependencies:

   ```sh
   uv sync --locked
   ```

3. Optional developer skill link into a project directory:

   ```sh
   python install.py --dev --target .
   ```

## Verification Checklist Before Opening a PR

Run all checks locally before pushing:

```sh
uv run ruff check
uv run pytest
uv build --wheel --out-dir dist skill/ultron/scripts
```

- **Secret scanning:** Run `gitleaks git --redact --config .gitleaks.toml` (or install `pre-commit install`) to ensure no secrets, API keys, or local personal paths are introduced.
- **Parity tests:** Heuristic changes must preserve the characterization suite (`tests/core/test_parity_heuristics.py`).
- **Quality changes:** Changes meant to improve relevance or ranking must pass the labelled benchmark gates (`uv run python tools/quality_benchmark.py compare`) and state the measured delta. See [docs/self-improvement.md](docs/self-improvement.md).
- **English codebase & docs:** All skill instructions (`skill/ultron/SKILL.md`, `skill/ultron/references/*.md`), code, comments, and documentation must be written in English. Multilingual keyword matching belongs strictly inside the dedicated rule/vocabulary data modules with regression tests.
- **Cross-platform paths & installers:** Keep `install.py`, `install.sh`, and `install.ps1` compatible with paths containing spaces and environments without Python pre-installed.

## Pull Request Guidelines

- Use a [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) title in the form `type(scope)!: summary`. Scope and `!` are optional. Scopes can contain letters, numbers, dots, underscores, slashes, hyphens, and single spaces between words. Use `feat` for a new capability, `fix` for a bug fix, and `!` for an incompatible change. Other accepted types are `build`, `chore`, `ci`, `docs`, `perf`, `refactor`, `revert`, `style`, and `test`.
- Keep package version changes in a dedicated release PR. Use a title such as `chore(release): prepare v0.1.1`.
- Use the [pull request template](.github/pull_request_template.md) to describe the change, link related issues, and record validation.
- Keep PRs focused and include regression tests for bug fixes or new ATS/feed behaviors.
- Never commit `.env` files, personal profiles/CVs, `~/.ultron` exports, or hardcoded local filesystem paths.
- Check GitHub Actions results (`lint`, `secrets`, `test`, and `install-smoke`) after pushing.

The Linux/Python 3.13 coverage job measures lines and branches in the `ultron` package. Open its Actions summary for the totals and the changed-line report on PRs. Download the `coverage-report` artifact for the HTML, XML, and JSON reports. The README badge shows combined line and branch coverage from the latest successful `main` measurement.

## Publishing a Release

1. Bump the package version in `skill/ultron/scripts/pyproject.toml` and `skill/ultron/scripts/ultron/__init__.py`. Refresh `uv.lock` with `uv lock` and submit the changes in a PR.
2. After the version PR merges and CI passes, tag that commit as `vX.Y.Z` and push the tag. The version in the tag should match the package version.
3. The [Release workflow](.github/workflows/release.yml) runs Ruff and pytest, builds the wheel, and publishes it with `SHA256SUMS.txt` on GitHub Releases. Verify that both files are attached to the release.

A merge updates the source available to the default bootstrap. A tag publishes a versioned release artifact. Users refresh existing installations with [`--update`](README.md#updating-ultron).
