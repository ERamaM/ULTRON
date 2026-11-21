# ULTRON

**U**niversal **L**ocator & **T**riage of **R**oles, **O**penings and **N**iches
(`Universal Locator & Triage of Roles, Openings and Niches`)

[![CI](https://github.com/ERamaM/ULTRON/actions/workflows/ci.yml/badge.svg)](https://github.com/ERamaM/ULTRON/actions/workflows/ci.yml)
[![Coverage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FERamaM%2FULTRON%2Fcoverage-badge%2Fbadge.json)](https://github.com/ERamaM/ULTRON/actions/workflows/ci.yml?query=branch%3Amain)
[![CodeQL](https://github.com/ERamaM/ULTRON/actions/workflows/codeql.yml/badge.svg)](https://github.com/ERamaM/ULTRON/actions/workflows/codeql.yml)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=ERamaM_ULTRON&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=ERamaM_ULTRON)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Secret Scanning: Gitleaks](https://img.shields.io/badge/secrets-gitleaks-blueviolet)](.gitleaks.toml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Agents](https://img.shields.io/badge/agents-Claude%20Code%20%7C%20Codex%20%7C%20OpenCode%20%7C%20Antigravity-black)](#supported-agents)

ULTRON is a production-ready agent skill and deterministic CLI for **Claude Code, Codex, OpenCode, and Antigravity**. It automates end-to-end job searches for any role family and geographic region: your coding agent supplies reasoning (CV-to-profile drafting, company discovery, web queries, ambiguous-role evaluation, salary research, and tailored cover letter drafting), while a local Python CLI handles high-volume ATS board queries, structured job feeds, strict filtering, scoring, deduplication, ledger history, `.docx` building, and exports.

## Highlights

- **Any role, any region:** Works across engineering, AI/ML, product, finance, operations, industrial R&D, and more, in Europe, the Americas, APAC, or global remote markets.
- **Zero-Python bootstrap:** `install.sh` and `install.ps1` automatically provision standalone `uv` and Python 3.12 if your machine does not have Python installed.
- **22 direct ATS integrations + structured feeds:** Fetches live openings straight from vendor APIs and aggregates public feeds via `discover feeds`.
- **Hybrid precision & triage:** Deterministic rules discard off-location/off-seniority noise before your agent spends tokens reviewing only genuinely uncertain ("unsure") or high-signal postings.
- **Incremental job ledger & exports:** Tracks seen roles in SQLite across runs, highlights new openings (`jobs new-since`), writes Markdown, JSON, Excel (`.xlsx`), and offline HTML reports, or opens a live read-only local web dashboard directly over `ultron.sqlite3` (`ultron export web`) without creating files.
- **Expected salary benchmarking:** Calculates sourced compensation ranges from verified non-internship CV tenure, run disclosures, and live market research (`ultron salary analyze`, `ultron salary save`).
- **ATS CV audit & `.docx` optimizer:** Audits `.docx`, PDF, and LaTeX `.tex` CVs for parser compatibility, verifies rewrite truthfulness against the source CV (`ultron cv verify-truth`), and generates a clean single-column Word `.docx` (`ultron cv build-docx`).
- **Tailored cover letter generator:** Pairs verified CV experience with job postings or profiles across bundled Word templates (`classic`, `modern`, `executive`) or custom `.docx` templates (`ultron cover-letter prepare`, `ultron cover-letter build-docx`).
- **Scheduled / zero-token mode:** Run unattended heuristic searches with `ultron run --unattended`.

## Documentation & User Wiki

Read the complete step-by-step documentation either in the **[Repository User Guide](docs/guide/README.md)** or on the **[GitHub Wiki](https://github.com/ERamaM/ULTRON/wiki)** (kept in sync automatically from `docs/guide/`):

| Guide | What You Will Learn |
| --- | --- |
| [Getting Started](docs/guide/getting-started.md) | Zero-Python installation, `doctor` verification, agent sandboxes, updating & uninstalling |
| [Search Profiles](docs/guide/search-profiles.md) | Creating profiles from a CV or interview, schema & seed packs, `explain`, diff edits & `undo` |
| [Searches & Presets](docs/guide/search-and-presets.md) | The 7-stage loop, choosing between `fast` / `balanced` / `deep` / `seed_boost`, custom overrides |
| [Unattended & Scheduling](docs/guide/unattended-and-scheduling.md) | Zero-token CLI runs, saved searches, `cron` / `launchd` / Task Scheduler / agent schedules |
| [Tracking & Exports](docs/guide/tracking-and-exports.md) | SQLite ledger, `jobs mark`/`note`/`feedback`, `new-since`, `.xlsx`, offline HTML, & live web viewer |
| [Career Tools](docs/guide/career-tools.md) | Salary estimation (`salary`), ATS CV optimization (`cv`), and cover letters (`cover-letter`) |
| [Troubleshooting & Data](docs/guide/troubleshooting-and-data.md) | `doctor`, `diagnose`, backups/cleanup/purge, privacy guarantees, common fixes |

## Supported Agents

Install once and invoke ULTRON in plain language from any of these coding agents (`RUNTIME.md` wires each skill copy to the isolated CLI environment):

| Agent | Skill Invoke | Default Global Install Path |
| --- | --- | --- |
| **Claude Code** | `/ultron ...` | `~/.claude/skills/ultron` |
| **Codex** | `$ultron ...` | `~/.agents/skills/ultron` |
| **OpenCode** | `ultron ...` | `~/.config/opencode/skills/ultron` |
| **Antigravity** | `ultron ...` | `~/.gemini/config/skills/ultron` |

## Installation

### Option 1 — Zero-Python Bootstrap (Recommended)

No pre-installed Python needed. If Python 3.12+ (with `venv` / `ensurepip`) is absent, the bootstrap script installs standalone [uv](https://docs.astral.sh/uv/) and provisions a managed Python 3.12 runtime automatically (no administrator or `sudo` privileges required):

**macOS / Linux:**

```sh
curl -fsSL https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.sh | sh
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.ps1 | iex
```

From a local clone, you can also run `./install.sh` (macOS/Linux) or `powershell -ExecutionPolicy Bypass -File .\install.ps1` (Windows).

### Option 2 — With Python 3.12+ Installed (From a Clone)

<!-- install:start -->
```bash
python install.py
```
<!-- install:end -->

On Linux/macOS use `python3 install.py` (on Debian/Ubuntu without `uv`, install `python3-venv` first). All installers enable the `cv` (PDF CV parsing via `pypdf`) and `xlsx` (Excel export via `openpyxl`) extras by default, auto-detect installed agents, and write `RUNTIME.md` next to `SKILL.md`. Restart your agent session after installing.

### Verify Installation & Common Installer Options

The `ultron` binary lives inside its dedicated venv (`~/.ultron/venv`) rather than mutating your global `PATH`. Run `doctor`:

- PowerShell: `& "$HOME\.ultron\venv\Scripts\ultron.exe" doctor`
- CMD: `"%USERPROFILE%\.ultron\venv\Scripts\ultron.exe" doctor`
- Linux/macOS: `~/.ultron/venv/bin/ultron doctor`

| Option | Effect |
| --- | --- |
| `--project DIR` | Install into `DIR/.claude` and `DIR/.agents` instead of user folders |
| `--platforms claude,codex` | Target specific agents (`claude`, `codex`, `opencode`, `antigravity`) |
| `--update` | Refresh the virtual environment and existing ULTRON installs |
| `--uninstall` | Remove ULTRON installs (add `--purge-data` to remove `$ULTRON_HOME` data) |
| `--dry-run` | Preview installer changes without modifying the venv, skills, or saved data |

### Updating ULTRON

After an interactive search, ULTRON checks once every 24 hours for a newer stable GitHub release (`ULTRON_DISABLE_UPDATE_CHECK=1` disables checks). Ask your agent to update (`ultron update` prepares the release and outputs `install.py --update` commands; your profiles, saved searches, and ledger stay intact), or update manually:

- **macOS / Linux:** `./install.sh --update` (or `curl -fsSL https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.sh | sh -s -- --update`)
- **Windows (PowerShell):** `powershell -ExecutionPolicy Bypass -File .\install.ps1 --update` (or `& ([scriptblock]::Create((irm https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.ps1))) --update`)

See [Getting Started — Updating ULTRON](docs/guide/getting-started.md#updating-ultron) for version pinning (`--to vX.Y.Z`), project scopes (`--project DIR`), and `ultron doctor` checks.

## How It Works

```mermaid
flowchart LR
    P["1. Profile"] --> S["2. Verify Companies"]
    S --> D["3. Feeds & Web Search"]
    D --> A["4. Query 22 ATS APIs"]
    A --> F["5. Fetch Postings"]
    F --> H["6. Deterministic Filter"]
    H --> E["7. Agent Eval & Finalize"]
```

1. **Profile:** Draft a search profile from a PDF/text CV, build one by answering a few questions, or adapt the bundled template. Refine it in chat ("no management roles", "add Rust"): every change is previewed as a diff and can be undone.
2. **Companies:** Propose target employers and verify their live ATS boards (`ultron seeds verify`). A profile without full seed pack coverage generates new companies in rounds until reaching the preset target (`ultron seeds plan`, then `ultron seeds verify --round`).
3. **Discovery:** Pull structured job feeds (`ultron discover feeds` reading `skill/ultron/data/feeds.yaml`) and run targeted web queries (`candidates add`).
4. **ATS Crawl:** Query verified boards across 22 ATS vendors (`discover ats`).
5. **Fetch:** Download job descriptions with vendor API enrichment fallbacks (`fetch`).
6. **Filter:** Apply deterministic role, seniority, language, remote-policy, and geo rules (`filter`).
7. **Evaluate & Finalize:** Have the agent evaluate doubtful (`unsure`) postings, rank results, persist history to `$ULTRON_HOME`, and emit `results.md`, `results.json`, and optional `.xlsx` and `.html` exports.

## Supported ATS Vendors (22) & Feeds

ULTRON queries job boards directly through vendor-scoped clients (preventing SSRF and tracking per-vendor rate limits) and enriches runs with public job feeds:

`Ashby` · `BambooHR` · `Breezy` · `CandidateManager` · `Comeet` · `Factorial` · `Greenhouse` · `HireHive` · `Homerun` · `iCIMS` · `JazzHR` · `Jobvite` · `Lever` · `Occupop` · `Personio` · `Pinpoint` · `Recruitee` · `SmartRecruiters` · `Taleo` · `Teamtailor` · `Workable` · `Workday`

Public structured feed and curated remote-company list endpoints live in `skill/ultron/data/feeds.yaml` and are executed by `discover feeds`.

## Search Presets

Presets control crawl depth, request budgets, company discovery breadth, and how aggressively borderline postings are sent to the agent for review (`balanced` is used by default unless you pass `--preset <name>`):

| Preset | Max Pages | Max Seeds | Web Queries | Ambiguity Threshold | New Companies Target | Best For |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `fast` | 70 | 120 | 20 | 55 | 10 | Quick checks on an established profile (`min_roles: 3`, `max_roles: 5`) |
| `balanced` | 400 | 180 | 40 | 50 | 25 | Default everyday balance between breadth, runtime, and agent tokens |
| `deep` | 4000 | 400 | 120 | 45 | 60 | Thorough market sweeps; rescues more borderline candidates (`Ambiguity Threshold` = 45) |
| `seed_boost` | 4000 | 400 | 220 | 45 | 120 | New, niche, or underrepresented profiles needing extra company discovery (`seed_target: 130`) |

- **Ambiguity Threshold:** Minimum heuristic score (0–100) for rescuing borderline non-hard-exclusion discards into the agent review queue; lower values keep more edge cases for agent evaluation.
- **New Companies Target:** Maximum newly discovered employers verified and added to your inventory per run. You can also override individual preset keys on saved searches with `ultron saved-search create ... --override KEY=VALUE` — see [Searches & Presets](docs/guide/search-and-presets.md).

## First Use

Open any supported agent and ask naturally:

- *"Create a search profile from my attached CV and find remote backend roles in Europe."*
- *"Run my saved search again and show only newly posted openings."*
- *"Estimate my expected salary from my CV and optimize my CV into an ATS-friendly Word document."*
- *"Write a tailored cover letter for this job posting using the modern Word template."*

If you ask for an interactive search without choosing a profile, ULTRON lists your options and asks which one to use, or ask *"How does ULTRON work?"* for a quick walkthrough.

### Unattended Mode

Use `--unattended` for scheduled or terminal-only runs with **zero AI tokens consumed**. ULTRON chains `discover feeds`, `discover ats`, `fetch`, `filter`, SQLite ledger persistence, and `finalize` automatically using your already-verified seeds and deterministic rules (skipping interactive web queries and LLM borderline evaluation):

```sh
ultron profile install-example
ultron run --unattended --profile ai-ml-remote-eu
```

Review newly discovered roles afterwards with `ultron jobs new-since <previous_run_id>` or automate runs with `ultron saved-search` and `cron` / `launchd` / Windows Task Scheduler — see [Unattended & Scheduling](docs/guide/unattended-and-scheduling.md).

## Data, Privacy & Security

- **Local-first storage:** Profiles, SQLite ledger, caches, and run outputs live exclusively in `$ULTRON_HOME` (default `~/.ultron`). ULTRON uploads no telemetry.
- **Provider visibility:** Your chosen agent provider processes only what the agent reads (profiles, CVs during profile/career steps, and shortlisted/ambiguous postings).
- **Redaction & untrusted input defense:** Exports redact API tokens/credentials automatically and sanitize Markdown, spreadsheet, and HTML output; custom Word templates use bounded ZIP reading and `defusedxml`. See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Development & CI

```sh
uv sync --locked
uv run ruff check
uv run pytest
```

Continuous integration runs Ruff linting, wheel build checks, Gitleaks secret scanning, pytest across Linux, macOS, and Windows (Python 3.12 and 3.13), and end-to-end installer smoke tests. CodeQL, SonarCloud, Dependabot, and Codex reviews (`AGENTS.md`, `CONTRIBUTING.md`) guard `main`.

## License

MIT License — see [LICENSE](LICENSE).
