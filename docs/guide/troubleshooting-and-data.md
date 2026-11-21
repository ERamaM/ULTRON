# Troubleshooting, Privacy & Data Maintenance

## Environment Health (`ultron doctor`)

Run `ultron doctor` whenever commands fail, network access looks blocked, or an update finishes:

```sh
ultron doctor                 # Full Python, $ULTRON_HOME, SQLite, extras, install & network check
ultron doctor --no-network    # Skip live DNS/HTTPS checks against ATS hosts
ultron doctor --public        # Strip local paths and error stack details before sharing output
```

Every failed check prints a `"fix"` field pointing to the relevant section in [references/platforms.md](../../skill/ultron/references/platforms.md).

## Common Issues & Fixes

| Symptom | Likely Cause | Fix |
| --- | --- | --- |
| Expected job URL missing from results | Filtered out by title exclusions, seniority, language, geo/remote rule, or closed status | Run `ultron diagnose "<url>" --profile <slug> [--run <run_id>]` to see the exact stage and rule that dropped it |
| Network / HTTPS errors in `discover` or `fetch` | Agent sandbox blocks outbound port 443 | Enable outbound HTTPS in your agent config (e.g. Codex `-c sandbox_workspace_write.network_access=true`) and re-run `ultron doctor` |
| `stage_order` error (exit code `3`) | Out-of-order manual stage execution | Follow stage order: `new` -> `discover ats`/`feeds` -> `fetch` -> `filter` -> (`evaluate`) -> `finalize` |
| Windows permission error opening outputs | Codex Windows sandbox runs as a separate user | Point `ULTRON_HOME` to a directory shared with your Windows user account |
| `truthfulness_failed` during `--strict` export | Draft CV or cover letter includes unverified metric, year, credential, or term | Revise the draft to remove unverified claims, or pass confirmed details via `--confirmed <file>` |
| Missing `pypdf` or `openpyxl` feature | Old custom venv without extras | Re-run `./install.sh --update` or `powershell -ExecutionPolicy Bypass -File .\install.ps1 --update` |

## Managing Local Data (`ultron data`)

ULTRON stores everything locally under `$ULTRON_HOME` (default `~/.ultron`):

- `profiles/` — YAML search profiles and version backups
- `runs/` — Per-run stage artifacts and reports (`results.md`, `results.json`, `.xlsx`, `.html`)
- `ultron.sqlite3` — Deduplicated jobs ledger, notes, review states, and company seed yield history
- `backups/` — Automatic SQLite snapshots (last 5 kept)
- `venv/` — Isolated Python CLI runtime

Destructive maintenance commands **preview changes by default** (`{"preview": true, "would_delete": [...]}`) and modify nothing until you re-run with the confirmation flag:

```sh
ultron data status                              # Disk paths, SQLite size, run & job counts
ultron data check                               # SQLite PRAGMA integrity_check + disk/DB sync check
ultron data backup                              # Create snapshot under $ULTRON_HOME/backups/
ultron data restore <backup.sqlite3> [--yes]    # Preview first; pass --yes to restore DB
ultron data cleanup --days 30 [--execute]       # Preview first; deletes finished runs older than 30 days
ultron data delete-run <run_id> [--yes]         # Preview first; deletes one run folder
ultron data purge [--yes] [--include-backups] [--force]
```

**Retention guarantees:**
- `cleanup` and `delete-run` remove old run folders while keeping your SQLite `jobs` history (`jobs list`, `jobs new-since`, and `seed_yield_runs` remain intact).
- `purge` never deletes `profiles/` or `venv/` and only removes unfinished recent runs with `--force`. If `$ULTRON_HOME` lacks ULTRON's `.ultron-home` marker, cleanup avoids deleting unrelated pre-existing files.

## Privacy & Security

- **Local-first storage:** No telemetry or background uploads. Your agent provider sees only what your agent explicitly reads (profiles, CVs during profile/career tasks, and fetched postings).
- **Redaction & untrusted-input safety:** Exports and job-file snippets automatically redact API keys/tokens (`core.privacy`), HTML and `.xlsx` reports prevent XSS/formula injection, ATS clients bind HTTP requests strictly to verified vendor hosts, and custom Word templates enforce ZIP-size limits and `defusedxml` parsing.

For reporting vulnerabilities, see [SECURITY.md](../../SECURITY.md).

Back to [ULTRON User Guide](README.md).
