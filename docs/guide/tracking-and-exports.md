# Tracking & Exports

## Incremental SQLite Ledger

Every finished run writes normalized job records into `$ULTRON_HOME/ultron.sqlite3`. Postings are canonicalized by URL identity (stripping tracking parameters) so the same job discovered across multiple runs updates `times_seen` and timestamps cleanly rather than creating duplicate rows.

Deleting old run folders with `ultron data cleanup` preserves the SQLite job history so deduplication and new-job detection keep working across months of searches.

## Managing Job States, Notes, and Feedback

Review and track opportunities across five lifecycle states:

- `unreviewed` (default)
- `shortlisted`
- `applied`
- `interview`
- `rejected`
- `archived`

Ask your agent naturally (*"mark #3 as applied and note I spoke with the recruiter"*, *"show only new jobs since yesterday"*) or use the CLI directly (accepting either the canonical `identity_key` or the job URL):

```sh
ultron jobs list [--state unreviewed|shortlisted|applied|interview|rejected|archived] [--profile <slug>] [--since-run <run_id>] [--new] [--limit 50]
ultron jobs new-since <previous_run_id>
ultron jobs mark <identity_key_or_url> <state>
ultron jobs note <identity_key_or_url> "Submitted tailored application"
```

Record quality feedback on individual postings to help diagnose filter gaps or false positives:

```sh
ultron jobs feedback <identity_key_or_url> relevant|not_relevant|uncertain [--reason <code_or_topic>] [--note "<text>"]
```


Use `--profile` when a relevance judgment belongs to a particular search profile. The vacancy's
application state is shared. A judgment saved without a profile is labelled unscoped.

```sh
ultron jobs feedback <identity_key_or_url> relevant --profile <slug>
ultron jobs list --profile <slug> --new --since-run <run_id>
ultron jobs new-since <run_id> --saved-search <name>
```

The scoped commands report which history they compared. An older baseline with profile
provenance alone uses profile history when it belongs to the selected saved search's profile.

## Inspecting Past Runs

```sh
ultron runs list                   # Date, profile, preset, completed stages, and kept counts
ultron runs show <run_id>          # Full run metadata and profile snapshot
```

## Shortlist Export Formats

Every finished run writes `results.md` and `results.json` inside `$ULTRON_HOME/runs/<run_id>/`. You can also generate rich formatted reports anytime (or during finalize with `ultron finalize --run <run_id> --xlsx --html`):

| Format | Command | What It Produces |
| --- | --- | --- |
| **Markdown** | Automatic on finalize | `$ULTRON_HOME/runs/<run_id>/results.md` concise shortlist table & summaries |
| **JSON** | Automatic on finalize | `$ULTRON_HOME/runs/<run_id>/results.json` structured output with redacted secrets |
| **Excel (`.xlsx`)** | `ultron export xlsx --run <run_id> [--out file.xlsx]` | Multi-sheet workbook (`openpyxl`) with a live dashboard, a filterable Roles table, and an employer rollup |
| **Offline HTML** | `ultron export html --run <run_id> [--out file.html]` | Single self-contained `results.html` file viewable offline in any browser with zero network requests and strict HTML escaping |
| **Live Web Viewer** | `ultron export web [--port <n>] [--run <run_id>] [--profile <slug>] [--no-open] [--editable]` | Local ledger with Cards, Table, Kanban, and Watch & Salaries. Read-only by default. |

The Excel Summary shows live counts as you change **My Status** in the Roles table. Pick **To Review**,
**Shortlisted**, **Applied**, **Interview**, or **Rejected**, and add comments in **My Notes**. The
Companies sheet groups open roles by employer. These edits stay in your workbook.

All exports neutralize formula prefixes, redact credentials, and validate links. The web viewer binds
to loopback and serves all assets locally. In read-only mode, it opens the ledger without migrating it.
With `ultron export web --editable`, you can update statuses, add notes and feedback, and manage
watched boards. Use the launch URL printed by the CLI. Press **?** for shortcuts. A status change
offers **Undo** for five seconds. The first edit on an older ledger backs it up before upgrading.
Use **Export CSV** or **Copy URLs** to export the currently filtered jobs.

Next: [Career Tools](career-tools.md).
