# Export and viewer redesign specifications

These specifications upgrade ULTRON's three user-facing exports — Excel (`ultron export xlsx`), offline HTML (`ultron export html`), and the local web ledger (`ultron export web`) — so they match the [agent toolkit](../agent-first/31-agent-toolkit.md) and turn ULTRON's outputs into daily job-search tools rather than raw data dumps.

Status: proposed. Each specification is scoped to land in its own focused pull request.

- [32 · Toolkit-aware export data and view model](32-toolkit-aware-export-data.md)
- [33 · Executive Excel workbook redesign](33-excel-workbook-redesign.md)
- [34 · Interactive offline HTML report](34-offline-html-report-redesign.md)
- [35 · Interactive web ledger and job-hunt CRM](35-interactive-web-ledger-and-crm.md)

## Why the current exports fall short

All three exports were built for the legacy sweep pipeline where every row carried a heuristic `match_score`, `match_band`, and `matched`/`missing`/`uncertain` requirement lists. With the agent toolkit ([31](../agent-first/31-agent-toolkit.md)), the agent judges postings directly and records `priority` (1 to 3), `eligibility`, `fit_signals`, publication dates, and disclosed salaries — with no heuristic `match_score`.

As a result, on toolkit runs today:

- **Excel** displays five empty scoring columns (`Match Score`, `Match Band`, `Matched`, `Missing`, `Uncertain`) across 19 wide columns, uses a fixed 45 pt row height, and lacks `Priority`, `Salary`, `Posted`, live formulas, charts, or a status tracker.
- **Offline HTML** renders `"No score"` on every card and an empty grey `Match Bands` bar, offers no priority/remote/salary chips, and gives the user no way to mark decisions while reviewing offline.
- **Web viewer** opens with an `Unscored` `Match Bands` chart, ignores `scout_verdicts`, `profile_job_feedback`, `watchlist`, and `watch_pending`, and rejects every write request (`405 Method Not Allowed`), forcing users back to the CLI for every status change or note.

## Delivery order

Implement in numerical order so each pull request builds cleanly on the previous one:

| Order | Spec | Scope |
|---|---|---|
| 1 | [32](32-toolkit-aware-export-data.md) | Propagate toolkit fields (`salary`, `published_at`), add shared `exports/view_model.py`, and enrich `exports/web_data.py` |
| 2 | [33](33-excel-workbook-redesign.md) | Redesign `ultron export xlsx`: live-formula Dashboard with charts, adaptive `Roles` Excel Table with `My Status` dropdown, and `Companies` sheet |
| 3 | [34](34-offline-html-report-redesign.md) | Redesign `ultron export html`: hero KPIs, priority/salary badges, filter chips, Cards/Table toggle, and offline `localStorage` triage with CLI sync commands |
| 4 | [35](35-interactive-web-ledger-and-crm.md) | Upgrade `ultron export web`: Kanban view, keyboard triage, Watch & Salaries tab, `interview` state (schema v9), and opt-in `--editable` local persistence |

Specifications [33](33-excel-workbook-redesign.md) and [34](34-offline-html-report-redesign.md) are independent after [32](32-toolkit-aware-export-data.md) and may be implemented in parallel in separate worktrees.

## Shared constraints

- **Backward compatibility:** Old run folders (`results.json` from 0.4.x sweep runs) and older SQLite ledgers (schema v1–v8) must still render cleanly without errors. Scored sweep runs keep their match-score rings, data bars, and band breakdowns.
- **Security and privacy:**
  - All posting, run, and ledger text is untrusted: sanitize and redact through `ultron.exports.sanitize` before rendering.
  - Only `http://` and `https://` URLs with a hostname become links. Never link `javascript:`, `data:`, or `file:` URLs.
  - Neutralize spreadsheet formula prefixes (`=`, `+`, `-`, `@`, after stripping control characters) in Excel and CSV exports.
  - Offline HTML makes zero network requests (`default-src 'none'`, `img-src 'none'`, static JS authorized strictly by its SHA-256 CSP hash, never interpolating data into `<script>`).
  - The web server binds strictly to loopback (`127.0.0.1`, `localhost`, `::1`), stays read-only by default, and gates mutations behind `--editable`, a per-session random token, `Origin`/`Host` validation, and strict payload limits.
- **Architecture and quality (`AGENTS.md` and `docs/architecture.md`):**
  - Keep modules under ~400–500 lines by splitting proactively by responsibility (never cross 1,000 lines). Update [docs/architecture.md](../../architecture.md) whenever a module is added or split.
  - Preserve one-way package dependencies (`tests/test_architecture.py`) and avoid cross-package private (`_name`) imports.
  - Write concise English docstrings for every public module, class, method, and function (`ruff` `D100`–`D103`).
  - Keep `openpyxl` optional (`xlsx` extra, imported lazily) and keep web assets stdlib/vanilla JS/CSS with zero build step or CDN dependencies.
  - Maintain `>= 80%` coverage on new and changed lines with standalone, `pytest-xdist`-safe tests on Windows, macOS, and Linux.

## Verification

For every pull request in this suite, run before opening and after each fix round:

```sh
uv run ruff check
uv run pytest
uv build --wheel --out-dir dist skill/ultron/scripts
```

Check `tests/test_architecture.py`, `tests/test_skill_files.py` (when CLI flags or references change), and verify determinism and security assertions for each export format.
