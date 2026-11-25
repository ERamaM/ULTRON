# Commands

Users can drive ULTRON with short commands grouped in five areas. A command is the words after the skill's name,
however the platform invokes the skill: `/ultron jobs new` (Claude Code), `$ultron jobs new` (Codex), or a plain
message such as `ultron jobs new` (OpenCode, Antigravity, or any agent that loads skills by itself). Treat all of them
the same way. Matching is case-insensitive and also accepts the user's language for the same word when the intent is
clear (for example `ultron new openings` = `jobs new`).

Anything that is not a command (for example `ultron find data roles in Madrid`) is a normal request: handle it
with the matching intent in SKILL.md.

## Shortcut reference

Use this table to interpret shortcuts the user already knows. For general help, follow the
conversational walkthrough in SKILL.md. Show syntax when the user explicitly asks for it.
A group without an action runs its default (marked "default").

```
ultron search [profile] [preset]      full search with the agent (default)
       search quick [profile] [preset] heuristic-only search, no web search or evaluation
       search watch [profile]         re-check the watched boards with no agent
       search saved [list|run|create|delete] [name]
ultron profile                        list profiles (default)
       profile show <profile>
       profile cv <path>              CV -> proposed profile -> user review -> save
       profile new                    interview -> proposed profile -> user review -> save
       profile edit <profile> [what]  change a profile in conversation, preview, confirm
       profile undo <profile>         restore the version before the last change
       profile companies [profile]    verified companies for a profile
ultron salary [cv|profile]            estimate expected salary from CV experience + market research
ultron cv audit <path>                audit CV formatting/ATS compatibility and build clean .docx
       cv optimize <path>             optimize CV truthfully for ATS and generate clean .docx
ultron jobs [filter]                  list saved jobs (default)
       jobs new                       jobs new since the last run
       jobs mark <job> <state> [note]
       jobs why <url>                 why a job did not show up
ultron runs                           list past runs (default)
       runs show <run>
       runs excel [run]
ultron system                         installation and network check (default)
       system data [status|check|backup|restore|cleanup|delete-run|purge]
       system update [version]        update ULTRON to the latest (or given) stable release
```

## What each command does

Run the CLI as SKILL.md explains (path from `RUNTIME.md`, one JSON object per command). For listing commands, show
the JSON as a short table or list in the user's language; never paste raw JSON unless asked.

### search

- `search [profile]`: follow the profile selection steps in SKILL.md, then `references/toolkit.md`
  (`ultron search`, web search, `ultron inspect`, `ultron shortlist save`).
- `search planned [profile] [preset]`: the planned board sweep, the table of the search loop in SKILL.md.
  Preset defaults to `balanced`. Other presets are `fast`, `deep`, and `seed_boost`.
- `search quick [profile] [preset]`: follow the same profile selection steps. Once the profile is
  selected, run `ultron run --unattended --profile <slug> [--preset <p>]` with the requested preset
  and summarize the counts and top matches. Use `balanced` by default.
  Tell the user it used heuristics only.
- `search watch [profile]`: `ultron watch --profile <slug>`; summarize the new postings per employer and the
  postings that closed, and offer a search to judge the new ones. `ultron watchlist list|add|remove --profile
  <slug>` shows or edits the watched boards (`references/toolkit.md`).
- `search saved ...`: `ultron saved-search list|run|create|delete`; see `references/scheduling.md`.

### profile

- `profile`: `ultron profile list`. If the list is empty, offer `profile cv <path>` or the bundled example
  (`ultron profile install-example`).
- `profile show <profile>`: `ultron profile show <slug>`; summarize roles, seniority, geography scope and languages.
- `profile cv <path>`: the CV-to-profile steps in `references/profile.md` (extract, draft, validate, show the user,
  save only after they confirm).
- `profile new`: the interview in `references/profile-interview.md`.
- `profile edit <profile> [what]`: "Refining a profile in conversation" in `references/profile.md`
  (`ultron profile edit <slug> ... --dry-run`, show the diff, apply after the user confirms).
- `profile undo <profile>`: `ultron profile undo <slug> --dry-run`, show the diff, then `ultron profile undo <slug>`.
- `profile companies [profile]`: `ultron seeds list --profile <slug>`; group by ATS vendor and show the count.

### salary

- `salary [cv|profile]`: follow `references/salary.md` (`ultron salary analyze` to inspect CV tenure
  and local run disclosures, web research on `research_queries`, then `ultron salary save --dry-run`
  and apply after user confirmation).

### cv

- `cv audit <path>` / `cv optimize <path>`: follow `references/cv-ats.md` (`ultron cv audit` for
  formatting/structure/keyword analysis, truthful rewrite, `ultron cv verify-truth`, and
  `ultron cv build-docx` to output a clean single-column `.docx`).

### cover-letter

- `cover-letter [job|profile]`: follow `references/cover-letter.md` (`ultron cover-letter templates`
  to inspect or export Word templates, `ultron cover-letter prepare` to assemble verified CV evidence and
  job requirements, `ultron cover-letter verify-truth` to check claims, and `ultron cover-letter build-docx`
  to export a tailored Word `.docx` cover letter).

### jobs

- `jobs [filter]`: `ultron jobs list` with the options that match the filter words: a state (`unreviewed`,
  `shortlisted`, `applied`, `rejected`, `archived`) → `--state`; a profile → `--profile`; a number → `--limit`;
  `new` alone means the `jobs new` command below.
- `jobs new`: take the latest finished run before the most recent one from `ultron runs list` and run
  `ultron jobs new-since <run_id>`; with a single run, show `ultron jobs list --new`.
- `jobs mark <job> <state> [note]`: `<job>` is a URL, an identity key, or a number from the last list you showed.
  Run `ultron jobs mark <key> <state>` and, with a note, `ultron jobs note <key> "<note>"`. Accept the user's words
  for states (e.g., applied → `applied`, discard/skip → `rejected`, keep/save/interested → `shortlisted`). For "this result is
  wrong/good" use `ultron jobs feedback`; see `references/jobs-ledger.md`.
- `jobs why <url>`: `ultron diagnose <url>`; explain the stage that dropped it, as `references/diagnose.md` says.

### runs

- `runs`: `ultron runs list`; show date, profile, preset, stages done and kept count, newest first.
- `runs show <run>`: `ultron runs show <run_id>`; summarize settings and counts (it prints the full profile; do not
  repeat it).
- `runs excel [run]`: `ultron export xlsx --run <run_id>` (latest finished run by default); give the file path.
- `runs html [run]`: `ultron export html --run <run_id>` (latest finished run by default); give the file path.
- `ledger web`: `ultron export web [--port <n>] [--run <run_id>] [--profile <slug>] [--no-open]`; starts a loopback-only read-only local web viewer over `ultron.sqlite3` without writing any output file, prints the URL in JSON, and runs until interrupted.

### system

- `system`: `ultron doctor`; list failed checks with their `fix`.
- `system report [bug|issue]`: prepare a public GitHub issue using `references/issue-reporting.md`.
  Show the draft before submitting it.
- `system update [version]`: follow "Updating ULTRON" below.
- `system data [action]`: `ultron data <action>`, `status` by default; see `references/data-maintenance.md`.
  Destructive actions (`cleanup`, `delete-run`, `purge`, `restore`) always run as a preview first and only execute
  after the user confirms the preview.

## Updating ULTRON

`ultron update --check` compares the installed version with the latest stable GitHub release
(`installed`, `latest`, `update_available`, `release_url`) and never changes anything. Drafts and prereleases
are ignored; offline it returns the error `update_check_failed`.

To update, ask the user first. If they agree:

1. Run `ultron update` (or `ultron update --to vX.Y.Z` for a specific stable release; never `main`). Add
   `--project DIR` only for an older project installation outside the current folder (newer ones are recorded in
   `installs.json`). It downloads and verifies that release's source into a new temporary folder and returns
   `commands`: one `install.py --update` per installed scope that needs it (global and/or project, with their
   platforms). `reason` is `cli_outdated`, or `stale_skill_copies` when the CLI is current but some skill copies
   are older. It never runs them, because the running installation cannot replace itself.
2. If `status` is `up_to_date`, `no_release`, `no_installs` or `source_checkout`, tell the user (`detail`) and stop. With
   `dev_links`, those linked folders follow their git checkout and are not updated. `pruned_records` lists recorded
   folders that were deleted by hand; the next installer run drops them from `installs.json`. Tell the user about
   each `manual` entry (a copy that cannot be updated safely, for example next to a `--dev` link).
   If `downgrade` is true, the requested version is older than the installed one: confirm with the user again.
3. Otherwise run each entry of `commands` as its own separate process, one after another, exactly as given.
   `argv` is authoritative; `display` is the same command quoted for PowerShell on Windows and for sh elsewhere.
   Set the variables in `env` if present. Check that each installer prints `"ok": true`.
4. Run `ultron doctor` and report any failed check.
5. Tell the user to restart the agent session so the updated skill is loaded. The folder in `cleanup` may be
   deleted afterwards.
