# Data maintenance

All commands act on `$ULTRON_HOME` (default `~/.ultron/`) and print one JSON object. Destructive commands are
previews by default: they print `{"preview": true, "would_delete": [...]}` and change nothing until you add the
explicit flag. Show the preview to the user and get their agreement before re-running with the flag.

| Command | What it does |
|---|---|
| `ultron data status` | Paths, database size, schema version, number of jobs, runs (database and disk), backups. |
| `ultron data check` | `PRAGMA integrity_check` plus runs that exist in only one place (`runs_in_db_without_folder`, `runs_on_disk_without_row`). |
| `ultron data backup` | Snapshot of the existing database (errors with `not_found` if there is none; it never creates one) to `backups/ultron-<timestamp>.sqlite3`; the 5 newest automatic backups are kept. |
| `ultron data restore <file> [--yes]` | Replaces the database with a backup. |
| `ultron data cleanup --days N [--execute]` | Deletes finished runs (those that reached `finalize`) older than N days. |
| `ultron data delete-run <run_id> [--yes]` | Deletes one run folder. |
| `ultron data purge [--yes] [--include-backups] [--force]` | Deletes the database, `runs/`, `seeds/auto_seeds.yaml` and `cv/`. |

## What is kept

- `cleanup` and `delete-run` remove the run folder and its row in the `runs` table. The job history (`jobs` and
  `job_occurrences`) is kept, so `jobs list` and "new since" keep working. Jobs may still point to a deleted run id
  in `first_seen_run_id`/`last_seen_run_id`; that is expected. The per-profile seed history (`seed_yield_runs`)
  that orders ATS boards is kept too.
- `purge` never touches `profiles/` or `venv/`. It keeps `backups/` unless you pass `--include-backups`.
- Unfinished runs are never removed by `cleanup`. `purge` refuses to run while an unfinished run younger than 24 hours
  exists; finish or delete that run, or pass `--force` only if the user confirms it is abandoned.

## Backups and restore

- `restore` checks the file first: it must open as SQLite, pass `PRAGMA integrity_check`, contain a `jobs` table and
  not be from a newer ULTRON. A bad file is rejected and nothing changes.
- Before replacing the database, `restore --yes` saves the current one as `backups/ultron-<timestamp>-pre-restore.sqlite3`
  (reported as `previous_backup`).
- Only the automatic `ultron-<timestamp>.sqlite3` backups are rotated. Migration copies (`...-pre-v2.sqlite3`) and
  `...-pre-restore.sqlite3` copies are not rotated; delete them by hand if you need the space.
- After restoring an older schema, the next command migrates the ledger forward (with its own backup).

## Safety guarantees

Every path is resolved and must lie inside `$ULTRON_HOME` (run deletion inside `runs/`); run ids must match
`YYYYMMDD-HHMMSS-xxxxxx`, so `../x` or absolute paths are rejected; a `runs/` folder that resolves outside the home is refused. In `purge`, a symlink inside the purged folders is removed as a link (its target is never followed or deleted); all targets are validated before anything is deleted. Do not
delete files under `$ULTRON_HOME` by hand when a command exists for it.

## Migrating from MARK III

`ultron import-mark3 <path> [--dry-run]` is a one-time import. `<path>` is MARK III's data folder (or its checkout, then
`<path>/data` is used) and must contain `mark_iii.sqlite3` or `mark_ii.sqlite3`. The source database is never opened in place: it is copied (with any -wal/-shm) to a temp folder and the copy is read, so nothing is written to the MARK III folder. Run `--dry-run` first: it only reports counts.

- Profiles: each `search_profiles` row becomes `$ULTRON_HOME/profiles/<slug>.yaml` (`criteria`, `query_templates`,
  `worked_examples` when present). Existing slugs are skipped (`skipped_slugs`); rows that fail validation are listed under
  `invalid`. A profile gets `criteria.vocabulary_packs: [ai-ml]` when a focus term in `criteria.role_focus_terms` contains an ai-ml role or ambiguity term as a whole word ("senior ML engineer"), or is itself a whole word inside a role term ("llm"); the slugs are reported as `vocabulary_pack_added`. Check them and edit the profile if the guess is wrong.
- Seeds: `ats_auto_seeds.yaml` is merged into `seeds/auto_seeds.yaml` with de-duplication, keeping every linked profile,
  `source_type` and `verification`. Seeds with no profile are linked to `mark3-import`.
- History: `jobs` and `job_feedback` are copied with `INSERT OR IGNORE` by `identity_key` (`first_seen_at`/`last_seen_at`
  map to `first_seen`/`last_seen`; `profile_slug` stays empty). Running it twice adds nothing.
- `missing_tables` lists what an older MARK III database lacked (for example `job_feedback`); the import still succeeds.
