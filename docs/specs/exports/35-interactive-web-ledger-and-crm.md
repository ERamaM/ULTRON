# 35 · Interactive web ledger and job-hunt CRM

Status: proposed. Depends on [32](32-toolkit-aware-export-data.md).

## Current behavior

`ultron export web [--host 127.0.0.1] [--port 0] [--run ID] [--profile SLUG] [--no-open]` ([exports/web_server.py](../../../skill/ultron/scripts/ultron/exports/web_server.py), [exports/web_data.py](../../../skill/ultron/scripts/ultron/exports/web_data.py), [exports/web_static/](../../../skill/ultron/scripts/ultron/exports/web_static/)) serves a read-only local dashboard over `$ULTRON_HOME/ultron.sqlite3`:

- Every mutating HTTP method (`POST`, `PUT`, `PATCH`, `DELETE`) returns `405 Method Not Allowed`. Users cannot mark a job `shortlisted`, `applied`, `rejected`, add notes, record feedback, or manage their watchlist from the browser.
- The Jobs view has Cards and Table modes only (no Kanban board), no keyboard shortcuts, and no filtered CSV export.
- The top visual strip highlights `Match Bands` (which shows 100% `Unscored` on toolkit runs), has no `Priority`, `Remote`, or `Salary` breakdowns, and has no view for `ultron watch` pending postings, recently closed postings, or the watchlist.
- `web_static/app.js` is already 864 lines and `web_static/app.css` is 724 lines — adding features directly to `app.js` would cross the 1,000-line ceiling.

## Expected behavior

Upgrade `ultron export web` into a fast, daily-driver personal job-hunt CRM with Kanban, keyboard triage, toolkit-native analytics, Watch & Salaries views, and safe opt-in local persistence (`--editable`) — while keeping the default `ultron export web` strictly read-only.

### A. Ledger schema v9: add `"interview"` review state

1. Extend `storage/review.py` `STATES` from 5 to 6 states:
   `("unreviewed", "shortlisted", "applied", "interview", "rejected", "archived")`.
2. In `storage/ledger.py`, bump `SCHEMA_VERSION` from `8` to `9` and add `_apply_v9(conn)`:
   - Runs inside the existing `BEGIN IMMEDIATE` transaction after `_backup(conn, 9)`.
   - Rebuilds `jobs` (preserving every column, index, and row from v8) with the updated `CHECK (review_state IN ('unreviewed', 'shortlisted', 'applied', 'interview', 'rejected', 'archived'))`.
3. Update `exports/web_data.py` `_STATES` and `commands/jobs.py` CLI choices to include `"interview"`.
4. Read-only mode (`ultron export web` without `--editable`) continues to open v1–v8 SQLite files without triggering any migration or write.

### B. Safe opt-in write mode (`ultron export web --editable`)

1. **CLI flag:** add `--editable` (default `False`) to `ultron export web` in `commands/export.py`.
   - Without `--editable`: server stays 100% read-only (`?mode=ro`, `405` on all non-`GET`/`HEAD` methods, UI renders cleanly with read-only badge and no mutating buttons enabled).
   - With `--editable`: server still binds **strictly to loopback** (`127.0.0.1`, `localhost`, `::1`, enforced by `validate_bind_target`), and enables authenticated local mutation endpoints.
2. **Defense-in-depth security for `--editable`:**
   - Generate one cryptographically random per-server-instance token (`secrets.token_urlsafe(32)`) on startup.
   - Inject the token ONLY into the served `/` (`index.html`) response (e.g., via `<meta name="ultron-csrf" content="...">` when `editable=True`, omitted in read-only mode) and never log it to stdout/stderr or store it on disk.
   - Every mutating endpoint (`POST`, `PATCH`, `PUT`, `DELETE`) MUST validate ALL of:
     1. Server is running with `editable=True` (else `405 Method Not Allowed`).
     2. `X-ULTRON-Token` header matches the session token via `hmac.compare_digest` (else `403 Forbidden`).
     3. `Host` header matches the bound loopback host/port, and if `Origin` is present it matches `http://<bound-host>:<bound-port>` (else `403 Forbidden`, defeating DNS rebinding and cross-site requests).
     4. `Content-Type` is `application/json` and `Content-Length` is present and `<= 16_384` bytes (else `415` / `413`).
     5. JSON body validates strictly against the endpoint's schema; invalid inputs return `400 Bad Request`.
   - All writes delegate directly to existing domain modules (`storage/review.py` and `storage/watchlist.py`) so business rules, validation, timestamps, and ledger invariants are never duplicated.
3. **Mutation API endpoints (`exports/web_api.py`, new module):**
   Use fixed JSON-body `POST` endpoints so `identity_key` (which may contain colons or `url://` slashes) never has to be URL-path-encoded:
   - `POST /api/jobs/state` — body `{"identity_key": "...", "state": "unreviewed|shortlisted|applied|interview|rejected|archived"}` -> calls `review.set_state` -> returns updated job summary + updated `state_counts`.
   - `POST /api/jobs/notes` — body `{"identity_key": "...", "text": "..."}` (non-empty, max 2,000 chars) -> calls `review.add_note` -> returns updated `notes`.
   - `POST /api/jobs/feedback` — body `{"identity_key": "...", "relevance": "relevant|not_relevant|uncertain", "reason_code": "...", "note": "...", "profile": "..."}` -> calls `review.set_feedback` -> returns updated feedback.
   - `POST /api/watchlist/add` — body `{"profile": "<slug>", "board": "<url or vendor:board>"}` -> resolves board key using a shared public helper promoted from `commands/watch.py` (never cross-importing `commands/`) and calls `watchlist.add` -> returns updated watchlist entry.
   - `POST /api/watchlist/remove` — body `{"profile": "<slug>", "seed_key": "<vendor:board>"}` -> calls `watchlist.remove` -> returns `{"removed": seed_key}`.
   - Expose `"editable": true|false` in `/api/ledger` `meta` so the frontend enables or hides write controls cleanly.
   - Reject `PUT`, `PATCH`, and `DELETE` with `405 Method Not Allowed` (and reject `POST` with `405` whenever `editable=False`).

### C. Modular frontend architecture (`exports/web_static/`)

Split the static assets before adding features so every file stays concise (< ~400–500 lines), zero-build, and loaded under `script-src 'self'` / `style-src 'self'`:

- `web_static/app.js`: state store, `/api/ledger` fetching, URL hash sync, tab routing, filter pipeline, and keyboard shortcut dispatcher.
- `web_static/views.js`: Cards view, Table view, company monogram rendering, clickable `fit_signals` chips, detail drawer, and CSV/URL export.
- `web_static/kanban.js`: Kanban pipeline board (`Unreviewed`, `Shortlisted`, `Applied`, `Interview`, `Rejected`) with accessible move controls and drag-and-drop when `editable`.
- `web_static/actions.js`: `--editable` API client (`X-ULTRON-Token`), optimistic UI updates, Undo toast (5 s rollback on state change), inline notes form, feedback buttons, and Watchlist add/remove controls.
- `web_static/charts.js`: adaptive visual summary strip (Priority / Match Band stacked bar, Remote Policy breakdown, Salary market bars, Top Companies) and the `Watch & Salaries` tab.
- `web_static/app.css` + `web_static/kanban.css`: clean styling, light/dark themes, density toggle (`Comfortable` / `Compact`), subtle transitions respecting `prefers-reduced-motion`.

Register every new static asset route in `web_server._load_static_routes()` from the package root (`ultron.__file__`).

### D. Daily-driver UX features

1. **Toolkit-first top analytics:**
   - Automatically switch the first visual panel to **Priority (`P1 / P2 / P3`)** when `summary.display_mode == "toolkit"`, show **Match Bands** when `"scored"`, and offer a compact toggle between both when `"mixed"`.
   - Show **Remote Policy** pills and **Review Pipeline** pills (including `Interview`) — all clickable to filter.
   - Keep `Top Companies` horizontal bars with company monogram avatars.
2. **Three Job views (`Cards` | `Table` | `Kanban`):**
   - `Cards`: company monogram, title, company + location, `Priority` badge (or score ring), `Salary` callout, `Posted` / `First seen` date, `Closed` warning pill when `is_closed_watch` is true, clickable `fit_signals` chips, and quick action buttons (`Shortlist`, `Applied`, `Interview`, `Reject`, `Open ↗`) when `--editable`.
   - `Table`: high-density sortable columns including `Priority`, `Salary`, `Posted`, `Remote`, and inline status dropdown when `--editable`.
   - `Kanban`: 5 columns (`Unreviewed`, `Shortlisted`, `Applied`, `Interview`, `Rejected`; `Archived` accessible via filter) with column counts; drag-and-drop + accessible button moves when `--editable`, read-only pipeline overview otherwise.
3. **Keyboard-first triage & Undo toast:**
   - Shortcuts (disabled while typing inside inputs/textareas):
     - `j` / `k`: move selection down / up
     - `Enter`: open/close detail drawer
     - `o`: open selected posting URL in a new tab
     - `/` : focus search input
     - `v`: cycle Cards / Table / Kanban view
     - `?`: toggle keyboard shortcuts help dialog
     - When `--editable`: `s` (`shortlisted`), `a` (`applied`), `i` (`interview`), `x` (`rejected`), `u` (`unreviewed`), `z` (undo last state change)
   - Every state transition displays a transient **Undo toast** (restoring the previous state via `POST /api/jobs/state`).
4. **New `Watch & Salaries` tab:**
   - **Watchlist panel:** lists watched and blocked boards per profile with source (`user` / `history`) and keep count; when `--editable`, includes an inline **"Add board"** form (`board URL` or `vendor:board`) and **"Remove"** button.
   - **Pending Watch postings:** new postings discovered by `ultron watch` awaiting the next search session.
   - **Recently Closed alerts:** postings from the latest shortlist that `ultron watch` detected as closed.
   - **Market Salaries panel:** horizontal range/median bars by currency from `summary.salary_summary` plus list of roles with disclosed pay.
5. **Filtered export actions:**
   - `Export CSV` button downloads the currently filtered jobs as a UTF-8 CSV file (neutralizing formula prefixes `=`, `+`, `-`, `@` on every cell).
   - `Copy URLs` button copies safe `http(s)` URLs of the currently filtered jobs to the clipboard.

### E. Documentation & skill consistency

- Update `docs/architecture.md` with `exports/web_api.py` and the split `web_static/` modules, updating `exports/` description to note opt-in `--editable` local persistence.
- Update `skill/ultron/references/commands.md`, `skill/ultron/references/jobs-ledger.md`, and `docs/guide/tracking-and-exports.md` to document `ultron export web --editable` and the `"interview"` review state.

## Files to touch

| File | Change |
|---|---|
| `skill/ultron/scripts/ultron/storage/review.py` | Add `"interview"` to `STATES` |
| `skill/ultron/scripts/ultron/storage/ledger.py` | Bump `SCHEMA_VERSION = 9`, add `_apply_v9` migration with backup |
| `skill/ultron/scripts/ultron/commands/jobs.py` | Accept `"interview"` in `jobs mark <identity_key> <state>` and `jobs list --state` |
| `skill/ultron/scripts/ultron/commands/export.py` | Add `--editable` flag to `ultron export web` |
| `skill/ultron/scripts/ultron/exports/web_api.py` | New module: CSRF/Origin/Host/body validation and mutation handlers (`state`, `notes`, `feedback`, `watchlist`) |
| `skill/ultron/scripts/ultron/exports/web_server.py` | Optional `editable=False`, session token injection, route dispatch to `web_api`, static routes for split JS/CSS |
| `skill/ultron/scripts/ultron/exports/web_static/*` | Split into `index.html`, `app.css`, `kanban.css`, `app.js`, `views.js`, `kanban.js`, `actions.js`, `charts.js` |
| `docs/architecture.md`, `docs/guide/tracking-and-exports.md`, `skill/ultron/references/commands.md`, `skill/ultron/references/jobs-ledger.md` | Document `--editable`, `interview` state, Kanban, Watch & Salaries tab |
| `tests/test_export_web.py`, `tests/test_ledger.py`, `tests/test_skill_files.py` | Tests for v8->v9 migration, read-only default (405), `--editable` CSRF/Origin/Host/size security checks, state/note/feedback/watchlist mutations, and static asset serving |

## Acceptance cases

- **Read-only default unchanged:** `ultron export web` without `--editable` still rejects `POST`/`PUT`/`PATCH`/`DELETE` with `405`, omits any CSRF meta tag, never modifies `ultron.sqlite3`, and opens v1–v8 ledgers cleanly.
- **Loopback enforcement:** `ultron export web --editable --host 0.0.0.0` is rejected with a validation error.
- **CSRF & Origin protection:** Under `--editable`, mutation requests missing `X-ULTRON-Token`, sending a wrong token, sending a non-loopback `Origin`, sending a non-JSON `Content-Type`, or exceeding 16 KB are rejected (`403` / `415` / `413`) with zero DB changes.
- **State, notes, feedback, and watchlist mutations:** Valid authenticated requests to `/api/jobs/state`, `/api/jobs/notes`, `/api/jobs/feedback`, `/api/watchlist/add`, and `/api/watchlist/remove` persist to SQLite through `storage/review.py` and `storage/watchlist.py` and are immediately reflected by `GET /api/ledger`.
- **Schema v9 migration:** Upgrading a v8 ledger backs up the database first, preserves all existing jobs/occurrences/feedback/scout_verdicts/watchlist rows, and allows setting `review_state = 'interview'`.
- **Static assets & CSP:** Every `/assets/*.js` and `/assets/*.css` route returns `200 OK` with strict `SECURITY_HEADERS` (`script-src 'self'`, `style-src 'self'`, `frame-ancestors 'none'`).
- **Module size & architecture:** Every Python module in `exports/` stays under ~500 lines and `tests/test_architecture.py` and `tests/test_skill_files.py` pass.

## Verification

Run `uv run ruff check`, `uv run pytest`, `uv run pytest tests/test_architecture.py tests/test_skill_files.py`, and `uv build --wheel --out-dir dist skill/ultron/scripts`. Smoke-test both `ultron export web --no-open` (read-only) and `ultron export web --editable` against a temporary `ULTRON_HOME` ledger in a browser to verify Cards, Table, Kanban, keyboard shortcuts, Undo toast, Watch & Salaries tab, and CSV export.
