# Unattended Mode & Scheduling

## What Unattended Mode Does

In **Unattended Mode** (`--unattended`), ULTRON executes an end-to-end search directly in Python **without an AI agent in the loop and with zero LLM tokens consumed**. In a single CLI invocation, it chains:

1. Creating a new run for your profile and preset.
2. Pulling structured feeds (`discover feeds`) and querying your verified ATS company boards (`discover ats`).
3. Downloading job postings (`fetch`).
4. Applying deterministic role, seniority, language, remote-policy, and location filters (`filter`).
5. Persisting new and updated occurrences in `$ULTRON_HOME/ultron.sqlite3` and emitting `results.md` / `results.json` (`finalize`).

### Interactive Mode vs. Unattended Mode

| Capability | Interactive (via AI Agent) | Unattended (`--unattended`) |
| --- | --- | --- |
| AI token usage | Uses agent tokens for judgment steps | **0 AI tokens** |
| Seed source | Existing seeds + new company discovery rounds | Existing verified seeds + `feeds.yaml` |
| Web search queries | Agent executes targeted web & `site:` queries | Skipped (uses ATS boards + structured feeds only) |
| Borderline (`ambiguous`) postings | Agent evaluates against rubric | Auto-handled deterministically (confident matches kept) |
| Best suited for | Initial setup, niche markets, weekly deep sweeps | Scheduled daily checks (`cron` / Task Scheduler / scripts) |

## Quick Terminal Run

If you have not created a custom profile yet, install the bundled example first, then launch an unattended run:

```sh
ultron profile install-example
ultron run --unattended --profile ai-ml-remote-eu
```

Pass `--preset` (`fast`, `balanced`, `deep`, or `seed_boost`) to control crawl depth:

```sh
ultron run --unattended --profile <slug> --preset balanced
```

Each invocation prints one JSON summary object containing the `run_id`, kept count, and export paths.

## Saved Searches

Create named, re-runnable searches (with optional preset parameter overrides) so recurring scripts and agents reference a stable name:

```sh
ultron saved-search create daily-backend --profile <slug> --preset fast --override max_pages=150
ultron saved-search list
ultron saved-search run daily-backend --unattended
ultron saved-search delete daily-backend
```

## Inspecting Results After a Scheduled Run

Use the job ledger after an unattended run completes to see only newly discovered roles since your previous run:

```sh
ultron runs list
ultron jobs new-since <previous_run_id>
ultron jobs list --new --profile <slug>
```

## Scheduling Rules (Read Before Automating)

Whichever scheduler you choose, follow these four rules:

1. **Run on the machine holding `$ULTRON_HOME`:** Cloud-hosted agent runners that do not mount your local `~/.ultron` directory start from an empty profile and ledger.
2. **Pass the full executable path:** Schedulers run with a minimal `PATH`. Point directly to `~/.ultron/venv/bin/ultron` (macOS/Linux) or `%USERPROFILE%\.ultron\venv\Scripts\ultron.exe` (Windows).
3. **Set `ULTRON_HOME` explicitly** if you keep your data outside `~/.ultron`.
4. **Avoid overlapping runs:** Never schedule two concurrent `ultron` write runs against the same `$ULTRON_HOME`.

## Scheduler Setup by Platform

### Linux (`cron`)

Edit your crontab (`crontab -e`) to run at 08:00 every weekday and append logs cleanly:

```cron
0 8 * * 1-5 ULTRON_HOME="$HOME/.ultron" "$HOME/.ultron/venv/bin/ultron" run --unattended --profile ai-ml-remote-eu >> "$HOME/.ultron/unattended.log" 2>&1
```

### macOS (`launchd`)

Create a user LaunchAgent plist under `~/Library/LaunchAgents/com.ultron.search.plist` with `ProgramArguments` set to `["/Users/<you>/.ultron/venv/bin/ultron", "run", "--unattended", "--profile", "ai-ml-remote-eu"]`, configure `StartCalendarInterval`, and load it with `launchctl load`.

### Windows (Task Scheduler)

1. Open **Task Scheduler** and choose **Create Basic Task**.
2. Set the schedule trigger (e.g., Daily at `08:00`).
3. Action: **Start a program**:
   - **Program/script:** `C:\Users\<you>\.ultron\venv\Scripts\ultron.exe`
   - **Add arguments:** `run --unattended --profile ai-ml-remote-eu`
4. Keep **Run only when user is logged on** unless `$ULTRON_HOME` is readable by the background account.

### Agent-Native Schedules (Claude Code & Codex)

- **Claude Code:** Run `/schedule` and supply `ultron run --unattended --profile <slug>`. Prefer local schedules when you want updates stored in your local `$ULTRON_HOME` ledger.
- **Codex:** Create an Automation running `ultron run --unattended --profile <slug>` in a local environment that has access to `$ULTRON_HOME` and outbound HTTPS.

For retention and cleanup after many scheduled runs, see [Troubleshooting & Data](troubleshooting-and-data.md).

Next: [Tracking & Exports](tracking-and-exports.md).
