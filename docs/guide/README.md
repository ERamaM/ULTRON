# ULTRON User Guide

**ULTRON** (**U**niversal **L**ocator & **T**riage of **R**oles, **O**penings and **N**iches) pairs an AI coding agent with a local deterministic Python CLI (`ultron`) to automate end-to-end job discovery, filtering, salary benchmarking, CV ATS optimization, and cover letter drafting.

- **Your AI agent** handles judgment: drafting a profile from your CV, proposing target companies, running web queries, evaluating borderline postings, researching live salary bands, and writing tailored cover letters.
- **The local CLI (`ultron`)** handles repeatable high-volume execution: querying 22 ATS vendor APIs and structured feeds, deduplicating openings in SQLite, applying deterministic filters, exporting Excel/HTML/Markdown reports, auditing CVs, and validating truthfulness.

## Quick Start (3 Minutes)

1. **Install ULTRON** (no Python pre-install needed):
   - macOS / Linux: `curl -fsSL https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.sh | sh`
   - Windows PowerShell: `irm https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.ps1 | iex`
2. **Verify your environment:** run `doctor` (see [Getting Started](getting-started.md#verify-the-installation)).
3. **Start using it from your agent:**
   - Open Claude Code (`/ultron`), Codex (`$ultron`), OpenCode (`ultron`), or Antigravity (`ultron`) and ask naturally, for example:
     - *"Create a search profile from my attached CV and find remote backend roles in Europe."*
     - *"Run my saved search again and show only newly posted openings."*
     - *"Estimate my expected salary from my CV and optimize my CV into an ATS-friendly Word document."*
     - *"Write a tailored cover letter for this job posting using the modern Word template."*

For scheduled or zero-token terminal usage, see [Unattended Mode & Scheduling](unattended-and-scheduling.md).

## Guide Directory

| Page | Covers |
| --- | --- |
| [Getting Started](getting-started.md) | Installers (`install.sh`, `install.ps1`, `install.py`), verifying with `doctor`, agent sandbox setup, updating ULTRON |
| [Search Profiles](search-profiles.md) | Creating profiles (CV, interview, example), fields & seed packs, inspecting with `explain`, diff-previewed edits & `undo` |
| [Searches & Presets](search-and-presets.md) | The 7-stage search loop, all 4 Search Presets (`fast`, `balanced`, `deep`, `seed_boost`), custom overrides, `diagnose` |
| [Unattended & Scheduling](unattended-and-scheduling.md) | Zero-token `--unattended` runs, interactive vs. unattended comparison, saved searches, `cron` / `launchd` / Task Scheduler / agent schedules |
| [Tracking & Exports](tracking-and-exports.md) | SQLite ledger deduplication, `jobs list/mark/note/feedback`, `jobs new-since`, Excel (`.xlsx`) & offline HTML exports |
| [Career Tools](career-tools.md) | Salary estimation (`ultron salary`), ATS CV audit & `.docx` optimizer (`ultron cv`), tailored cover letters (`ultron cover-letter`) |
| [Troubleshooting & Data](troubleshooting-and-data.md) | `doctor`, `diagnose`, `$ULTRON_HOME` backups/cleanup/purge, privacy & redaction, common fixes |

## Prompt vs. CLI Cheat Sheet

| Goal | What to ask your agent | Direct CLI command |
| --- | --- | --- |
| Create profile from CV | *"Build a search profile from my CV"* | `ultron cv extract <cv_path>` then `ultron profile save --from <file>` |
| Install bundled example | *"Try ULTRON with the example profile"* | `ultron profile install-example` |
| Run an interactive search | *"Find roles matching my profile"* | `ultron run new --profile <slug> [--preset balanced]` |
| Run unattended search | *"Run an unattended search with zero agent tokens"* | `ultron run --unattended --profile <slug> [--preset balanced]` |
| Inspect new jobs | *"Show me only new openings since last time"* | `ultron jobs new-since <run_id>` |
| Export shortlist | *"Export my last run to Excel / HTML"* | `ultron export xlsx --run <run_id>` / `ultron export html --run <run_id>` |
| Benchmark salary | *"What salary range fits my CV?"* | `ultron salary analyze --cv <cv_path>` |
| Optimize CV for ATS | *"Audit my CV and export a clean .docx"* | `ultron cv audit <cv_path>` / `ultron cv build-docx --from <draft> --strict` |
| Generate cover letter | *"Write a cover letter for this job"* | `ultron cover-letter prepare` / `ultron cover-letter build-docx --from <draft> --strict` |

Looking for repository setup or architecture internals? See the [Repository README](../../README.md), [Architecture Overview](../architecture.md), and [Skill Commands Reference](../../skill/ultron/references/commands.md).
