# 32 · Toolkit-aware export data and view model

Status: proposed. Prerequisite for [33](33-excel-workbook-redesign.md), [34](34-offline-html-report-redesign.md), and [35](35-interactive-web-ledger-and-crm.md).

## Current behavior

- `FinalRow` ([core/models.py](../../../skill/ultron/scripts/ultron/core/models.py)) carries `priority`, `found_by`, `published_at`, `updated_at`, `unverified_date`, and `first_seen`, but does not carry disclosed pay (`salary`). When `ultron shortlist save` converts verified finds into `FinalRow` rows ([stages/scout_verified.py](../../../skill/ultron/scripts/ultron/stages/scout_verified.py), [toolkit/shortlist.py](../../../skill/ultron/scripts/ultron/toolkit/shortlist.py)), cached `posting_date` and extracted salary disclosures from `inspect` are not propagated onto the saved shortlist rows.
- `exports/xlsx.py`, `exports/html_report.py`, and `exports/web_data.py` each implement their own formatting for bands, eligibility, remote policies, and dates, and assume `match_score` / `match_band` are the primary ranking signal.
- `exports/web_data.py` ([exports/web_data.py](../../../skill/ultron/scripts/ultron/exports/web_data.py)) reads `jobs`, `job_occurrences`, `job_feedback`, `runs`, `seed_yield_runs`, `saved_searches`, and `diagnostics`. It ignores:
  - `scout_verdicts` (which holds `priority`, `verdict`, and `row_json` with `remote_policy`, `eligibility`, `eligibility_evidence`, `fit_signals`, `found_by`, and `salary` for toolkit runs),
  - `profile_job_feedback` (profile-scoped relevance feedback added in schema v7),
  - `watchlist` and `watch_pending` (watched boards and pending watch postings),
  - watch digests under `$ULTRON_HOME/watch/` (which list recently closed postings from `ultron watch`).

## Expected behavior

### A. Propagate `salary` and publication dates onto `FinalRow` and saved shortlists

1. Add an optional `salary: str = ""` field to `FinalRow` in `core/models.py` (default `""`, preserving compatibility with all existing `results.json` files and fixtures).
2. In `stages/scout_verified.py`:
   - Accept optional `salary` (bounded to 120 chars) and `published_at` (ISO date `YYYY-MM-DD`, validated/truncated safely) on verified find rows.
   - Copy `salary` and `published_at` onto `FinalRow` in `final_row(...)`.
3. In `toolkit/shortlist.py` (`_with_cache`):
   - When filling a row from the `inspect` cache (`inspected.jsonl`), populate default `published_at` from `job.get("posting_date")` (when valid) and default `salary` from the cached job text via `salary.disclosure.extract_salaries_from_text` (or cached `salary` string if present), unless the agent explicitly supplied them.
   - Redact/sanitize `salary` with `ultron.core.privacy.redact` / clean text so untrusted page text never leaks secrets or control characters.
4. In `core/final_row.py` (`assemble_final_row`, used by sweep runs):
   - Optionally populate `salary` from `job.body_text` using the same shared disclosure formatter if a disclosure exists, keeping existing sweep benchmark/parity gates unchanged (`tools/quality_benchmark.py compare` and `tools/measure_quality.py` must show zero delta).

### B. Shared export view model (`ultron/exports/view_model.py`)

Create a new domain helper module `skill/ultron/scripts/ultron/exports/view_model.py` (and document it in `docs/architecture.md`) used by `xlsx`, `html_report`, and `web_data` so formatting rules live in one place:

- **Display mode detection (`detect_export_mode(rows)`):**
  - Returns `"scored"` when at least one row has a non-null `match_score` or a valid `match_band` in `("strong", "good", "borderline", "ineligible")`.
  - Returns `"toolkit"` when rows carry `priority` (or `found_by == "agent_verified"`) and have no `match_score`.
  - Returns `"mixed"` when both scored and toolkit rows appear (for example in the multi-run web ledger).
- **Priority formatting (`normalize_priority(value)` / `priority_label(priority)`):**
  - Accepts `1`, `2`, `3` (anything else returns `None`).
  - Maps to canonical tokens `p1` (`"P1 · Apply first"`), `p2` (`"P2 · Strong"`), `p3` (`"P3 · Backup"`).
- **Date formatting (`best_vacancy_date(row)`):**
  - Prefers `published_at`, then `updated_at`, then `first_seen`, then `unverified_date`, then `freshness` (when ISO-like).
  - Returns `{"iso": "YYYY-MM-DD", "kind": "published" | "updated" | "first_seen" | "unverified", "label": "..."}` or `None`.
- **Company monogram (`company_monogram(company)`):**
  - Deterministic 1–2 uppercase ASCII/Unicode initials (e.g., `"Acme Corp"` -> `"AC"`, `"n8n"` -> `"N8"`, empty -> `"?"`) and a deterministic palette index `0..7` computed with `hashlib.sha256` (never Python's salted `hash()`, so exports remain 100% deterministic across runs and platforms).
- **Fit-signal tags (`split_fit_signals(value, max_tags=8)`):**
  - Splits lists or comma/pipe-separated strings into clean, deduplicated, sanitized short tags (<= 60 chars each).
- **Remote label (`remote_label(remote_policy)`):**
  - Maps `remote_spain` -> `"Remote · Spain"`, `remote_eu` -> `"Remote · EU"`, `remote_europe` -> `"Remote · Europe"`, `remote_emea` -> `"Remote · EMEA"`, `remote_other` -> `"Remote · Other"`, else `""`.
- **Salary helper (`format_salary_disclosure(body_text)` and `parse_salary_midpoint(salary_text)`):**
  - Shared single-line formatter (e.g., `"EUR 80,000–110,000 / year"` or `"EUR 95,000 / year"`) handling single-bound disclosures safely (no `round(None)`).
  - Returns optional numeric `(currency, midpoint_annual)` for sorting and median KPIs when unambiguous.
- **Export sanitization reuse:** every text output flows through `ultron.exports.sanitize` (`clean_text`, `clean_multiline_text`, `safe_http_url`, `clamp_score`, `normalize_token`).

### C. Enrich read-only SQLite snapshot (`ultron/exports/web_data.py`)

Keep `build_ledger_snapshot(db_path)` strictly read-only (`?mode=ro`, `PRAGMA query_only = ON`) and resilient to older schema versions (v1–v8 where newer tables or columns may not exist):

1. **Per-job toolkit enrichment (from `scout_verdicts` when present):**
   - Join or lookup latest `scout_verdicts` row per `(identity_key, profile_slug)` (falling back to latest `recorded_at` for `identity_key`).
   - Attach sanitized fields to each job object: `priority` (`1 | 2 | 3 | None`), `verdict` (`"keep" | "unsure" | "discard" | ""`), `remote_policy`, `remote_label`, `eligibility`, `eligibility_evidence`, `fit_signals` (list of tags), `salary`, `found_by`, `best_date` (from `view_model.best_vacancy_date`), and `monogram` (`{"initials": ..., "tone": 0..7}`).
2. **Profile-scoped feedback (from `profile_job_feedback` when present):**
   - Return `profile_feedback` mapping `{profile_slug: {"relevance", "reason_code", "note", "labeled_at"}}` alongside the existing top-level `feedback_*` fields (which resolve to the job's `profile_slug` feedback first, then legacy unscoped `job_feedback`).
3. **Watchlist, pending watch items, and closed-posting alerts:**
   - Read `watchlist` table (`profile_slug`, `seed_key`, `company`, `state`, `added_by`, `added_at`) and computed history-backed boards (reusing read-only queries; never mutating the DB).
   - Read `watch_pending` rows with `state = 'pending'` (`profile_slug`, `identity_key`, `title`, `company`, `location`, `url`, `found_at`, `source_key`).
   - Optionally read the latest watch digest JSON per profile under `$ULTRON_HOME/watch/` (strictly inside the watch folder, rejecting symlinks, bounded to a safe file size) to surface recently detected `closed` postings (`identity_key`, `title`, `company`, `url`, `profile_slug`, `detected_at`) and flag matching ledger jobs with `is_closed_watch: true`.
4. **Summary additions:**
   - `display_mode` (`"toolkit" | "scored" | "mixed"`),
   - `priority_counts` (`{"p1": n, "p2": n, "p3": n, "unset": n}`),
   - `remote_counts` (count per normalized `remote_policy`),
   - `salary_summary` (disclosed job count + per-currency `{count, min, median, max}` for jobs with parseable annual bands),
   - `watchlist_count`, `watch_pending_count`, `closed_watch_count`.

## Files to touch

| File | Change |
|---|---|
| `skill/ultron/scripts/ultron/core/models.py` | Add `salary: str = ""` to `FinalRow` |
| `skill/ultron/scripts/ultron/core/final_row.py` | Populate optional `salary` in `assemble_final_row` without altering filter/rank behavior |
| `skill/ultron/scripts/ultron/stages/scout_verified.py` | Accept and carry optional `salary` and `published_at` |
| `skill/ultron/scripts/ultron/toolkit/shortlist.py` | Fill `published_at` and `salary` from inspect cache when omitted |
| `skill/ultron/scripts/ultron/exports/view_model.py` | New shared view-model helpers |
| `skill/ultron/scripts/ultron/exports/web_data.py` | Enrich read-only snapshot with `scout_verdicts`, `profile_job_feedback`, `watchlist`, `watch_pending`, watch closed alerts, and summary breakdowns (split helper queries into `exports/web_queries.py` if `web_data.py` exceeds ~450 lines) |
| `docs/architecture.md` | Document `exports/view_model.py` (and `exports/web_queries.py` if added) |
| `tests/test_export_view_model.py`, `tests/test_export_web.py` | Unit tests covering all view-model helpers, toolkit snapshot enrichment, schema v1–v8 compatibility, and redaction |

## Acceptance cases

- `FinalRow` serializes and deserializes with or without `salary`, and old `results.json` files without `salary` or `priority` load without error.
- `ultron shortlist save` fills `published_at` and `salary` from the inspect cache when the agent saves by `id`, and respects explicit values when supplied by the agent.
- `company_monogram("Acme Corp")` returns the exact same initials and tone across repeated calls and separate Python processes (`PYTHONHASHSEED`-independent).
- `detect_export_mode` returns `"toolkit"` for unscored rows with `priority`, `"scored"` for sweep rows with `match_score`, and `"mixed"` when both exist.
- `format_salary_disclosure` handles single-bound ranges (e.g., `"up to EUR 120,000"`), dual-bound ranges, and empty text without raising.
- `build_ledger_snapshot` opens a read-only database containing schema v1, v5, v6, v7, and v8 tables without failing or modifying the file (`mtime` unchanged).
- `build_ledger_snapshot` surfaces `priority`, `fit_signals`, `eligibility`, `remote_policy`, `salary`, `profile_feedback`, `watchlist`, `watch_pending`, and sanitized `closed_watch` items, redacting any embedded secrets and rejecting unsafe URLs.
- `tools/quality_benchmark.py compare` and `tools/measure_quality.py` report zero regression.

## Verification

Run `uv run ruff check`, `uv run pytest`, `uv run pytest tests/test_architecture.py`, `uv run python tools/quality_benchmark.py compare`, and `uv run python tools/measure_quality.py`.
