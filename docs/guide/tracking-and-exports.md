# Tracking & Exports

## Incremental SQLite Ledger

Every finished run writes normalized job records into `$ULTRON_HOME/ultron.sqlite3`. Postings are canonicalized by URL identity (stripping tracking parameters) so the same job discovered across multiple runs updates `times_seen` and timestamps cleanly rather than creating duplicate rows.

Deleting old run folders with `ultron data cleanup` preserves the SQLite job history so deduplication and new-job detection keep working across months of searches.

## Managing Job States, Notes, and Feedback

Review and track opportunities across five lifecycle states:

- `unreviewed` (default)
- `shortlisted`
- `applied`
- `rejected`
- `archived`

Ask your agent naturally (*"mark #3 as applied and note I spoke with the recruiter"*, *"show only new jobs since yesterday"*) or use the CLI directly (accepting either the canonical `identity_key` or the job URL):

```sh
ultron jobs list [--state unreviewed|shortlisted|applied|rejected|archived] [--profile <slug>] [--since-run <run_id>] [--new] [--limit 50]
ultron jobs new-since <previous_run_id>
ultron jobs mark <identity_key_or_url> <state>
ultron jobs note <identity_key_or_url> "Submitted tailored application"
```

Record quality feedback on individual postings to help diagnose filter gaps or false positives:

```sh
ultron jobs feedback <identity_key_or_url> relevant|not_relevant|uncertain [--reason <code_or_topic>] [--note "<text>"]
```

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
| **Excel (`.xlsx`)** | `ultron export xlsx --run <run_id> [--out file.xlsx]` | Multi-sheet workbook (`openpyxl`) with styled columns, filters, and safe clickable job URLs |
| **Offline HTML** | `ultron export html --run <run_id> [--out file.html]` | Single self-contained `results.html` file viewable offline in any browser with zero network requests and strict HTML escaping |
| **Live Web Viewer** | `ultron export web [--port <n>] [--run <run_id>] [--profile <slug>] [--no-open]` | Zero-file-write local loopback web server (`127.0.0.1`) serving a live read-only dashboard straight from `$ULTRON_HOME/ultron.sqlite3` |

All exports sanitize formula injections, redact credentials, and restrict hyperlinks strictly to `http`/`https` schemes. The live web viewer binds strictly to loopback addresses, opens the database read-only (`?mode=ro`), enforces a strict Content Security Policy with zero external assets, and updates immediately when clicking **Refresh**.

Next: [Career Tools](career-tools.md).
