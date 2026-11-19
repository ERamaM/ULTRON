# Jobs ledger

Every finished run records its jobs in the ledger (`ultron.sqlite3`). A job is identified by a canonical identity key
(tracking parameters are ignored), so the same posting seen in several runs is one row with `times_seen`. Review
states: `unreviewed` (default), `shortlisted`, `applied`, `rejected`, `archived`. Relevance labels: `relevant`,
`not_relevant`, `uncertain`.

## Commands

- `ultron jobs list [--state S] [--profile SLUG] [--since-run RUN_ID] [--new] [--limit N]` lists jobs
  (default limit 50). `--new` keeps only jobs seen in a single run.
- `ultron jobs new-since <run_id>` lists jobs first seen after that run.
- `ultron jobs mark <identity_key_or_url> <state>` sets the review state. Passing the job URL also works.
- `ultron jobs note <identity_key_or_url> "<text>"` appends a note.
- `ultron jobs feedback <identity_key_or_url> <relevance> [--reason CODE] [--note TEXT]` records whether the job was
  relevant; use it when the user says a result was good or bad.

Errors come back as `{"error": "not_found" | "invalid_value", ...}` with a non-zero exit code.

## Suggested use

1. After a run, show new jobs with `jobs new-since <previous_run_id>` and ask the user which to shortlist.
2. Mark decisions with `jobs mark` as the user reports them; keep context in notes, not in chat memory.
3. Use `jobs feedback` for jobs that should not have appeared, then follow `references/diagnose.md` for missed jobs.

The history survives run cleanup (see `references/data-maintenance.md`).

## Reading and exporting runs

- `ultron runs show <run_id>` prints the whole run record including the full profile; prefer `ultron runs list`.
- `ultron export xlsx --run <run_id> [--out <file.xlsx>]` writes the run's results as an Excel workbook (needs the
  optional Excel dependency; if it is missing the command says so and the `results.md`/`results.json` files remain).
  Use this command for Excel requests on every provider. The CLI owns the sheets, columns, styles, filters, and
  hyperlinks. Agent evaluations can change which rows are exported. Re-exporting the same run with the same ULTRON
  version preserves the workbook content and formatting. File timestamps can differ.
