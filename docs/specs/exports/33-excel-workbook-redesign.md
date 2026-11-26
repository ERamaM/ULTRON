# 33 · Executive Excel workbook redesign

Status: proposed. Depends on [32](32-toolkit-aware-export-data.md).

## Current behavior

`ultron export xlsx --run <run_id> [--out PATH]` ([exports/xlsx.py](../../../skill/ultron/scripts/ultron/exports/xlsx.py)) produces three sheets (`Summary`, `Roles (N)`, `Discards (D)`) plus an optional `Vacancy Dates` sheet:

- `Roles (N)` writes 19 fixed columns in a sweep-era order (`Company`, `Title`, ..., `Match Score`, `Match Band`, `Matched`, `Missing`, `Uncertain`, `Apply URL`, `New`, `Apply`). On toolkit runs, five scoring columns and `Freshness` are completely blank while `Priority`, `Salary`, and `Posted` are missing.
- Every data row uses a hardcoded `_ROW_HEIGHT = 45`, which truncates long evidence cells and wastes vertical space on short rows.
- `Summary` contains static counts only (no live formulas, no charts, no status tracking).
- `Roles (N)` uses manual borders and autofilter instead of an official Excel Table, and offers no user status dropdown or notes column.

## Expected behavior

Redesign `ultron export xlsx` into a clean, adaptive, executive workbook that looks polished immediately upon opening in Microsoft Excel and LibreOffice Calc, while staying deterministic and safe against formula injection.

### A. Module split

Split Excel generation cleanly by responsibility so every module stays well under ~400 lines (and update [docs/architecture.md](../../architecture.md)):

- `exports/xlsx_styles.py`: color palette (accessible blue/amber/slate tokens), fills, fonts, alignments, pill definitions (`Priority`, `Match Band`, `Eligibility`, `My Status`), and formula-injection neutralization helpers.
- `exports/xlsx_dashboard.py`: `Summary` executive dashboard sheet (live-formula KPI cards, run metadata, embedded native `openpyxl` charts, and discard breakdown).
- `exports/xlsx.py`: public entry point `export_run_to_excel`, `XlsxUnavailable`, adaptive `Roles (N)` sheet, `Companies (M)` sheet, `Discards (D)` sheet, and optional `Vacancy Dates` sheet.

All `openpyxl` imports remain lazy inside functions so ULTRON works when the `xlsx` extra is not installed.

### B. Sheet 1 — `Summary` (Executive Dashboard)

Keep the sheet name `"Summary"` (sheet 1, gridlines hidden) and upgrade its layout into an executive dashboard:

1. **Header banner:** Title (`ULTRON — Job Search Report`), profile slug + preset + finalized timestamp subtitle, and sanitized run prompt/description.
2. **Live KPI cards (top strip, driven by real Excel formulas referencing the `Roles` sheet):**
   - `Total Roles`: `=COUNTA('Roles (N)'!D2:D<last>)` (or `0` when empty)
   - `New Roles`: `=COUNTIF('Roles (N)'!<new_col>2:<new_col><last>, "New")`
   - `Top Priority` (toolkit mode: `P1` count via `COUNTIF`) / `Strong Matches` (scored mode: `strong` count via `COUNTIF`)
   - `With Salary`: count of non-empty `Salary` cells via `COUNTA`
   - **Live application tracker KPIs** bound directly to the `My Status` column on `Roles (N)`:
     - `Shortlisted` (`=COUNTIF(..., "Shortlisted")`)
     - `Applied` (`=COUNTIF(..., "Applied")`)
     - `Interview` (`=COUNTIF(..., "Interview")`)
     - `Rejected` (`=COUNTIF(..., "Rejected")`)
   - Changing `My Status` in `Roles (N)` updates the Dashboard counters live inside Excel.
3. **Breakdown tables + native `openpyxl` charts** (placed cleanly to the right of metadata with zero overlap):
   - **Chart 1:** Roles by Priority (`P1`, `P2`, `P3`) in toolkit mode, or Roles by Match Band (`Strong`, `Good`, `Borderline`, `Ineligible`, `Unscored`) in scored mode (`DoughnutChart` or horizontal `BarChart`, using the workbook's accessible blue/amber/slate series colors).
   - **Chart 2:** Roles by Remote Policy (`Remote · Spain`, `Remote · EU`, `Remote · Europe`, `Remote · EMEA`, `Remote · Other`, `Unspecified`) as a compact horizontal `BarChart`.
   - **Chart 3:** Top Companies (up to 8 employers by open role count) as a horizontal `BarChart`.
   - Omit charts cleanly when `rows` is empty (avoiding corrupt empty chart XML in Excel).
4. **Compact Run Facts & Discards by Reason** tables neatly aligned below the KPI strip.

### C. Sheet 2 — `Roles (N)` (Adaptive Action Table)

1. **Action-first column layout:**
   - Core columns (always shown):
     1. `#` (1-based rank, integer, centered, narrow `6`)
     2. `New` (`"New"` text label for locale independence, centered, width `8`)
     3. `Company` (width `24`, bold)
     4. `Title` (width `42`; **clickable `http(s)` hyperlink directly on the title cell** when the URL is safe, styled cleanly so the user can open any role immediately without scrolling right)
     5. `Location` (width `22`)
     6. `Remote` (human-friendly label from `view_model.remote_label`, width `16`)
     7. `Salary` (formatted disclosure from `view_model`, width `20`)
     8. `Posted` (native Excel `date` cell with number format `yyyy-mm-dd` when `best_vacancy_date` yields a valid ISO date, sortable chronologically; width `13`)
     9. `Eligibility` (pill-formatted: `confirmed`, `high`, `medium`, `unknown`, width `14`)
     10. `Eligibility Evidence` (width `38`)
     11. `Fit Signals` (clean bullet/tag-joined text, width `34`)
     12. `Verdict` (`keep` / `unsure`, blue/amber fill, width `11`)
     13. `My Status` (default `"To Review"`, centered, width `15`, with Excel **Data Validation dropdown**)
     14. `My Notes` (blank editable column for user notes, width `30`)
     15. `Apply` (short bold `"Apply ↗"` link when URL is safe, centered, width `11`)
     16. `Apply URL` (raw clickable URL at the far right for copy/paste, width `44`)
   - **Adaptive conditional columns (inserted only when at least one row populates them):**
     - `Priority` (`P1`, `P2`, `P3` with distinct accessible pills: deep blue / soft blue / slate; placed right after `#` whenever any row has `priority`).
     - `Match Score` (native numeric + data bar `0..100`) and `Match Band` (pill-formatted) — included whenever any row has `match_score` or a non-unknown `match_band`.
     - `Matched`, `Missing`, `Uncertain` — included only when at least one row populates those requirement lists.
     - `Freshness`, `Source Type`, `Source` — included when populated (omitting empty boilerplate columns like `"Agent find"` if all rows share empty `freshness`).
   - Result: on a toolkit run, the user sees a crisp ~14–16 column actionable sheet with zero dead scoring columns; on a scored sweep run, every scoring column and data bar remains intact.
2. **Official Excel Table (`openpyxl.worksheet.table.Table`):**
   - Wrap the `Roles` data range (`A1:<last_col><last_row>`, when `rows` is non-empty) in a named Excel Table (`RolesTable`, `TableStyleMedium2`, `showRowStripes=True`) with built-in filter/sort dropdowns.
   - Freeze panes below the header and after `Company` (`E2` when `Priority` is present, `D2` otherwise) so Rank, Priority/New, and Company stay visible while scrolling horizontally.
3. **Interactive `My Status` dropdown + conditional row styling:**
   - Add `DataValidation(type="list", formula1='"To Review,Shortlisted,Applied,Interview,Rejected"', allow_blank=True)` across the `My Status` column.
   - Add conditional formatting rules:
     - `Shortlisted` -> soft blue pill on `My Status`
     - `Applied` -> deep blue pill on `My Status` + subtle row tint
     - `Interview` -> rich teal/blue bold pill on `My Status`
     - `Rejected` -> muted grey pill + subtle grey italic/strikethrough on the row so rejected roles recede visually without being deleted.
4. **Dynamic row heights:**
   - Compute each row's height from the maximum estimated wrapped lines across its text columns (`ceil(len(text) / max(1, col_width - 2))` plus explicit newline breaks), multiplied by `15 pt` + `8 pt` padding, clamped to `[22, 96] pt`.
   - Center short/badge columns vertically (`vertical="center"`) and top-align multi-line text columns (`vertical="top"`).

### D. Sheet 3 — `Companies (M)` (Employer Rollup)

Add a third sheet summarizing every distinct employer in `rows` (omitted or shown with `- no rows -` when `rows` is empty):

- Columns: `Company`, `Open Roles` (integer), `New Roles` (integer), `Best Priority` (or `Top Score` in scored mode), `Remote Policies` (unique labels), `Locations` (deduplicated preview), `Sample Role`, `Careers / Board Link` (board URL derived from `source_key` or first safe `apply_url`, clickable `http(s)` only).
- Sorted by `Open Roles` descending, then `Best Priority` ascending, then `Company` A–Z.
- Wrapped in an official Excel Table (`CompaniesTable`) with dynamic row heights and frozen header row.

### E. Sheet 4 — `Discards (D)` & optional `Vacancy Dates`

- Upgrade `Discards (D)` to use an official Excel Table (`DiscardsTable`) and dynamic row heights while preserving its existing columns (`Reason`, `Stage`, `Company`, `Title`, `Location`, `Remote`, `Detail`, `URL`).
- Preserve the optional `Vacancy Dates` sheet whenever rows carry date provenance (`first_seen`, `published_at`, `updated_at`, `unverified_date`).

### F. Security, determinism, and print setup

- Neutralize formula prefixes (`=`, `+`, `-`, `@`) after stripping C0 control characters in every user-data cell (never neutralize ULTRON's own `Summary` KPI formulas, which only reference static sheet/column coordinates).
- Never create hyperlinks for non-`http(s)` URLs (`javascript:`, `file:`, `data:`).
- Keep repeated exports of the same run byte-for-byte identical in the `.xlsx` ZIP archive (aside from `docProps/core.xml` timestamps), preserving the existing determinism test.
- Landscape fit-to-1-page-wide print setup with repeating row 1 header on every table sheet.

## Files to touch

| File | Change |
|---|---|
| `skill/ultron/scripts/ultron/exports/xlsx_styles.py` | New module: palette, styles, pills, neutralization, dynamic row-height estimator |
| `skill/ultron/scripts/ultron/exports/xlsx_dashboard.py` | New module: `Summary` sheet with live KPI formulas and native `openpyxl` charts |
| `skill/ultron/scripts/ultron/exports/xlsx.py` | Adaptive `Roles (N)` Excel Table with `My Status` validation, `Companies (M)` sheet, `Discards (D)` table |
| `docs/architecture.md` | Document `xlsx_styles` and `xlsx_dashboard` under `exports/` |
| `docs/guide/tracking-and-exports.md` | Document the new Dashboard, `My Status` dropdown, and `Companies` sheet |
| `tests/test_xlsx.py`, `tests/test_xlsx_layout.py` | Update column-letter assertions for adaptive layouts and add tests for toolkit mode, scored mode, live formulas, charts, `DataValidation`, dynamic row heights, `Companies` sheet, and determinism |

## Acceptance cases

- Exporting a **toolkit run** (`priority` present, `match_score` absent) includes `Priority`, `Salary`, `Posted`, `My Status`, and `My Notes`, and omits empty `Match Score`, `Match Band`, `Matched`, `Missing`, and `Uncertain` columns.
- Exporting a **scored sweep run** (`match_score` present) retains `Match Score` (numeric + DataBarRule) and `Match Band` (band pills) alongside the new `My Status` dropdown and `Companies` sheet.
- `Title` cells carry working `http(s)` hyperlinks for safe URLs and no hyperlink for `javascript:` / `file:` URLs; formula-prefixed titles (`=HYPERLINK(...)`) remain neutralized strings (`data_type == "s"`).
- `Summary` contains live Excel formulas (`=COUNTA(...)`, `=COUNTIF(...)`) referencing `Roles (N)` and embeds at least one `openpyxl` chart when `rows` is non-empty, and zero charts when `rows` is empty.
- `Roles (N)` defines a valid Excel Table (`ws.tables`) and a `DataValidation` dropdown on `My Status` with the five status values.
- `Posted` writes native Excel date values (`datetime.date`) with `yyyy-mm-dd` format for valid ISO dates, and falls back to neutralized text otherwise.
- Row heights vary dynamically between short single-line rows and long multi-line evidence rows, staying within `[22, 96]`.
- `Companies (M)` aggregates multiple vacancies from the same employer into one row with accurate role counts and best priority.
- Two consecutive exports of the same run produce identical ZIP entries except `docProps/core.xml`.
- Missing `openpyxl` still raises `XlsxUnavailable` cleanly.

## Verification

Run `uv run ruff check`, `uv run pytest tests/test_xlsx.py tests/test_xlsx_layout.py tests/test_architecture.py`, the full `uv run pytest`, and `uv build --wheel --out-dir dist skill/ultron/scripts`. Manually inspect one generated `.xlsx` from a toolkit run and one from a sweep run in Excel or LibreOffice to verify chart placement and column widths.
