# 34 · Interactive offline HTML report

Status: proposed. Depends on [32](32-toolkit-aware-export-data.md).

## Current behavior

`ultron export html --run <run_id> [--out PATH]` ([exports/html_report.py](../../../skill/ultron/scripts/ultron/exports/html_report.py)) generates a single offline HTML file with strict CSP (`default-src 'none'`, `img-src 'none'`, SHA-256 hashed inline script).

Limitations today:

- On toolkit runs (`match_score` is `None`), every job card shows `"No score"` on the right rail and the header `Match Bands` bar is an empty grey strip.
- `fit_signals` renders as a plain comma-separated paragraph rather than scannable tags, and `salary` and `published_at` are not surfaced prominently.
- Filtering is limited to one text box, a `New only` checkbox, and a three-option sort dropdown (`Rank`, `Score`, `Company`).
- There is no compact table view, no way to mark or dismiss postings while reviewing offline, and no way to carry offline triage decisions back to the ULTRON CLI.

## Expected behavior

Redesign `ultron export html` into a crisp, modern, interactive offline report that preserves 100% offline operation, zero external assets, strict SHA-256 CSP, accessibility, and clean print/PDF layout.

### A. Module split

Split HTML generation cleanly so neither file approaches the 400–500 line soft ceiling (and update [docs/architecture.md](../../architecture.md)):

- `exports/html_assets.py`: the static CSS stylesheet (`REPORT_CSS`), the static vanilla JS (`REPORT_JS`), and the precomputed SHA-256 CSP header (`CSP`, computed from `REPORT_JS` at import time; zero untrusted data is ever interpolated into `REPORT_JS`).
- `exports/html_report.py`: data sanitization, hero KPI/distribution rendering, card and table row builders, discard section, and `export_run_to_html`.

### B. Executive header & clickable visual breakdown

1. **Hero KPI strip (4 compact tiles, tabular numerals):**
   - `Roles kept` (total count)
   - `New since last run` (highlighted badge when `> 0`)
   - `P1 · Apply first` (in toolkit mode) or `Strong matches` (in scored mode)
   - `Disclosed pay` (count of roles with salary; subtitle shows median annual pay when computable, or `Discarded` count when no role discloses pay)
2. **Adaptive clickable distribution bar:**
   - **Toolkit mode:** segmented bar & legend by **Priority** (`P1`, `P2`, `P3`) with accessible blue/indigo/slate fills.
   - **Scored mode:** existing segmented bar & legend by **Match Band** (`Strong`, `Good`, `Borderline`, `Ineligible`, `Unscored`).
   - Clicking any segment or legend button filters the job list to that bucket (click again to clear; reflected with `aria-pressed="true"`).
3. **New employers callout (optional compact strip):**
   - When new roles introduce employers not seen in earlier runs (`is_new` rows), show a compact collapsible `<details>` listing the new companies in this run with counts.

### C. Upgraded job cards (and compact Table view)

1. **Deterministic company monogram avatar:**
   - Render a clean CSS badge (`.avatar.tone-0` .. `.avatar.tone-7`) with the company's 1–2 letter monogram from `view_model.company_monogram` — pure HTML/CSS, zero external images (`img-src 'none'` stays enforced).
2. **Toolkit-aware right rail (no more `"No score"` wall):**
   - When `match_score` is present: render the existing conic-gradient score ring.
   - When `match_score` is absent and `priority` is present: render a high-contrast **Priority badge** (`P1` deep blue, `P2` soft blue, `P3` muted slate, with label `"Apply first"` / `"Strong"` / `"Backup"`).
   - When `salary` is present: render a bold **salary callout** directly above the `Apply` button.
   - Never print `"No score"` when a Priority badge is shown.
3. **Scannable metadata pills & clickable `fit_signals` tags:**
   - Metadata row: `New` pill (warm highlight), `Remote` label pill (`Remote · Spain`, etc.), `Eligibility` pill, and `Posted` / `First seen` date pill (`2026-11-25`).
   - `fit_signals`: render as individual `<button type="button" class="tag">` chips (clicking a tag fills the search box or activates a tag filter; clicking again clears it).
   - Keep `Eligibility evidence` and `Requirements` (`Matched`/`Missing`/`Uncertain`) inside clean `<details>` disclosures.
4. **View switcher (`Cards` | `Table`):**
   - Toolbar button toggles between the editorial Cards layout and a dense, scannable **Compact Table** (columns: `#`, `Priority`/`Score`, `Company`, `Title`, `Location`, `Remote`, `Salary`, `Posted`, `Triage`, `Apply`). Both views share the exact same filters, sort order, and triage state.

### D. Filter chips & improved sorting

- **Quick filter chips bar** (hidden without JS, enabled by `REPORT_JS`):
  - `All`, `New only`, `P1` (toolkit) / `Strong` (scored), `With salary`, `Shortlisted` (from offline triage), plus dynamic chips for the top 3 `Remote` policies present in the run.
- **Sort dropdown:**
  - `Rank` (default, preserving ULTRON's exact ranked order),
  - `Priority` (when toolkit rows exist),
  - `Score` (when scored rows exist),
  - `Posted date` (newest first, undated last),
  - `Salary` (highest annual midpoint first, undisclosed last),
  - `Company` (A–Z).
- **Theme toggle (`Light` / `Dark`):** respects `prefers-color-scheme` by default and allows manual toggle stored in `localStorage`.

### E. Offline triage (`localStorage`) + CLI sync helper

Because the report is a standalone offline file, users can still review and triage postings right in the browser with zero server:

1. **Per-posting triage controls (progressive enhancement, hidden without JS):**
   - Three compact toggle buttons per card/row: `Shortlist` (`shortlisted`), `Applied` (`applied`), `Dismiss` (`rejected`), plus a collapsible one-line personal note input.
   - State persists in `localStorage` under key `ultron:html-triage:<run_id>` (wrapped in `try/catch` so strict privacy modes or `file://` storage restrictions degrade cleanly without errors).
   - Dismissed cards dim visually (with a `"Hide dismissed"` checkbox in the toolbar).
2. **"Sync to ULTRON" drawer/dialog (pure client-side):**
   - A toolbar button (`Export triage (N)`) opens a clean native `<dialog>` (or collapsible panel) showing:
     - Ready-to-run shell commands (`ultron jobs mark <identity_key> <state>` and `ultron jobs note <identity_key> "<note>"`, matching `commands/jobs.py` with shell-safe quoting!) for every triaged role,
     - A **"Copy commands"** button (`navigator.clipboard.writeText` with fallback textarea selection),
     - A **"Reset local triage"** button with confirmation.
   - Every card stores its sanitized `data-id` (`identity_key`), `data-priority`, `data-remote`, `data-salary-mid`, `data-date`, and `data-tags` attributes so `REPORT_JS` operates purely on DOM attributes without any embedded JSON script injection.

### F. Security, accessibility, and print

- **Strict CSP preserved:** `default-src 'none'; style-src 'unsafe-inline'; script-src 'sha256-...'; img-src 'none'; base-uri 'none'; form-action 'none'`.
- **Zero XSS surface:** every text field passes through `sanitize.clean_text` and `html.escape(..., quote=True)`. `REPORT_JS` uses `textContent` and DOM APIs exclusively (never `innerHTML` with posting text).
- **Accessibility:** keyboard-navigable controls, visible focus outlines, `aria-pressed` on filter/triage buttons, `aria-live="polite"` result counter, `prefers-reduced-motion` support.
- **Print/PDF stylesheet:** hides interactive toolbars, triage buttons, and sync panels; prints clean rank, title, company, priority/score, salary, remote policy, fit tags, and visible URLs.

## Files to touch

| File | Change |
|---|---|
| `skill/ultron/scripts/ultron/exports/html_assets.py` | New module: static CSS, static JS, and SHA-256 `CSP` constant |
| `skill/ultron/scripts/ultron/exports/html_report.py` | Hero KPIs, toolkit/scored adaptive cards & table view, monograms, fit-signal chips, `data-*` attributes for offline triage |
| `docs/architecture.md` | Document `exports/html_assets.py` under `exports/` |
| `docs/guide/tracking-and-exports.md` | Document interactive offline filters, Cards/Table toggle, and `localStorage` triage sync |
| `tests/test_html_report.py` | Update/expand tests for toolkit mode (no `"No score"` when `priority` is set), scored mode, KPIs, salary/date pills, fit tags, CSP SHA-256 integrity, XSS/URL escaping, and empty-run rendering |

## Acceptance cases

- Exporting a **toolkit run** renders `P1`/`P2`/`P3` badges, priority distribution bar, salary callout (when disclosed), date pill, and company monogram — and never displays `"No score"` on rows that carry a `priority`.
- Exporting a **scored sweep run** preserves the conic-gradient score ring and `Match Bands` distribution bar.
- Every `<script>` tag in the generated HTML matches the `sha256-...` hash in its `<meta http-equiv="Content-Security-Policy">` tag, and the document references zero external URLs for stylesheets, scripts, fonts, or images.
- Untrusted `<script>alert(1)</script>` payloads in `title`, `company`, `fit_signals`, `salary`, `eligibility_evidence`, or `identity_key` are fully HTML-escaped and never appear unescaped in markup or script blocks.
- Unsafe `apply_url` values (`javascript:`, `file:`, `data:`) render as `"No safe link"`, never `<a href=...>`.
- `fit_signals` render as individual tag elements; `data-priority`, `data-remote`, `data-salary-mid`, `data-date`, and `data-id` attributes are present for client-side filtering, sorting, and triage command generation.
- Empty runs render the empty-state message cleanly with no broken scripts or division-by-zero errors.

## Verification

Run `uv run ruff check`, `uv run pytest tests/test_html_report.py tests/test_architecture.py`, and the full `uv run pytest`. Open generated HTML reports for both a toolkit run and a scored run directly via `file://` in a browser to verify CSP compliance (zero console errors), filtering, Cards/Table switching, dark mode, offline triage, and print preview.
