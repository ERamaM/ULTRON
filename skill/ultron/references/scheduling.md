# Unattended runs and scheduling

## Unattended mode

`ultron run --unattended --profile <slug> [--preset <name>]` chains discovery, fetch, filter and finalize in one call
with no agent judgment in the loop. It uses the verified seed inventory and the deterministic filters only, then
records jobs in the ledger. For a saved search use `ultron saved-search run <name> --unattended`
(create it with `ultron saved-search create <name> --profile <slug> [--preset P] [--override KEY=VALUE]`).

Each call prints one JSON object. Read the new jobs afterwards with `ultron jobs new-since <last_run_id>`.

## Scheduling

Schedule the command above. Whatever tool you use, check these first:

- The scheduler must run on the machine that has the ULTRON install and the user's `$ULTRON_HOME`. Cloud
  schedulers run in a separate environment and do not see the local `$ULTRON_HOME`, profiles or ledger; a cloud task
  would start from an empty state. Use them only if the user intends to keep state there.
- Set `ULTRON_HOME` explicitly in the scheduled environment if it is not the default `~/.ultron`.
- Use the full path to the `ultron` executable (schedulers often have a minimal `PATH`).
- Run at most one unattended run at a time per `$ULTRON_HOME`.

### Claude Code

Use the `/schedule` command and give it the command to run (`ultron run --unattended --profile <slug>`). Follow the
prompts it shows. If the schedule runs in the cloud, the warning above applies. Check the tool's own help for the
options it supports.

### Codex

Create an automation in Codex that runs the same command, using its automations interface. Make sure it runs in a
local environment with access to `$ULTRON_HOME`.

### Linux and macOS

Use `cron` (`crontab -e`, one line with the schedule, then the full command) or, on macOS, a `launchd` job
(a plist in `~/Library/LaunchAgents` whose program arguments are the command and whose schedule key sets the time).
Redirect output to a log file so failures are visible.

### Windows

Use Task Scheduler: create a basic task, choose the trigger, and set the action to start `ultron` with arguments
`run --unattended --profile <slug>`. Enable "run whether user is logged on or not" only if `ULTRON_HOME` is
reachable by that account.

## After scheduling

Tell the user how to check results (`ultron jobs new-since <run_id>`, `ultron data status`) and how to clean up old
runs (`references/data-maintenance.md`).

## Managing saved searches

- `ultron saved-search list` shows the saved searches; `ultron saved-search delete <name>` removes one.
