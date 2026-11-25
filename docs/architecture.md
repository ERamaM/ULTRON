# Architecture

The `ultron` package lives in `skill/ultron/scripts/ultron`. It is split into packages by
responsibility, and the dependencies between them point one way. `tests/test_architecture.py`
enforces the rules below on every import.

## Packages

| Package | Responsibility |
|---|---|
| `cli.py` | Entry point: builds the argument parser from `commands/` and dispatches. |
| `commands/` | One module per command group (`ultron cv`, `ultron runs`, ...) plus `output` (JSON output and errors). A command parses arguments, calls domain code and prints the result. |
| `infra/` | Network and host plumbing with no ULTRON domain knowledge: the HTTP client (`net`), SSRF guard (`urlpolicy`), pinned DNS transport, atomic filesystem & link helpers (`fs`), OOXML builders (`ooxml`), data folders (`home`), agent platform paths (`platforms`) and a read-only view of installs (`installs`). |
| `core/` | Domain logic that reads no run state: models, run presets, ATS clients and identity, extractors, feeds, rules, decisions, ranking and Markdown rendering. |
| `profiles/` | Search profiles: loading and saving (`store`), conversational edits (`edit`) and replaying a run with a draft profile (`explain`). |
| `cv/` | CV parsing (`extract`, `docx_reader`, the sandboxed PDF `worker`), the ATS audit (`ats`, `ats_target`, `ats_requirements`), experience timeline, truthfulness check and the `.docx` writer. |
| `cover_letter/` | Cover-letter brief preparation, bundled & custom `.docx` template rendering, truthfulness check and builder. |
| `salary/` | Expected-salary analysis from run postings. |
| `storage/` | Persistent state under the ULTRON home: the job ledger, run folders, saved searches, review states, maintenance (purge, export), the memory of agent verdicts (`scout_memory`) and the watchlist with its pending postings (`watchlist`). |
| `seeds/` | Company seeds: the inventory and bundled packs, the auto-seed file, company and domain names, verification and seed-generation rounds. |
| `stages/` | The search pipeline (discover, feeds, fetch, filter, evaluate, finalize, with `final_helpers` shared by both finalize paths), the unattended run, the per-run HTTP client factory, and what the agent toolkit builds on: find resolution (`scout_resolve`), recording judged postings (`scout_finds`, `scout_verified`) and finalizing a saved shortlist (`scout_finalize`), the role-searchable job APIs and keyless feeds with their cache (`role_feeds`), the watched boards (`watch_boards`) and the agent-free day-2 watch (`watch`). |
| `toolkit/` | The agent toolkit: compact posting dossiers (`dossier`), `inspect` (many postings, boards and companies at once, cached for saving) `shortlist save` (the agent's judged rows finalized with memory, history and exports) (`shortlist`) `search` (the watch's pending postings, watched boards and role-searchable job APIs) (`search`) and the stdio MCP server exposing them (`mcp_server`). |
| `exports/` | Read/view exports: shared export sanitization (`sanitize`), the Excel workbook (`xlsx`), the offline HTML report (`html_report`), and the local read-only SQLite web viewer (`web_data`, `web_server`, `web_static`). |
| `diagnostics/` | `doctor` (installation checks) and `diagnose` (run diagnostics). |
| `update/` | Release lookup and notices (`notice`), installed copies (`copies`), the installer command lines (`installer`), the release archive download (`archive`) and the orchestration (`prepare`). |
| `migrations/` | One-off imports from older projects (MARK III). |

## Rules

- `infra` imports nothing else from `ultron`.
- `core` imports only `core` and `infra`.
- Only `cli.py` and `commands/` import `commands/`. Logic that another module needs belongs in a
  domain package, not in a command (for example the web-search query plan lives in
  `core/discovery/query_plan.py`).
- No module grows past 1,000 lines. Split a module by responsibility before it reaches that size.
- A package's `__init__.py` is empty unless it is a documented facade. `seeds` is one: it
  re-exports the public names of its modules. Patch a function where it is defined (for example
  `seeds.inventory.packs_dir`), not on the facade.
- Modules locate bundled files from the package root (`ultron.__file__`), never by counting parent
  folders of their own file, so they can move between packages.
