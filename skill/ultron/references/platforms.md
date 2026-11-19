# Platform setup: network and shell commands

ULTRON needs two things from your agent platform: permission to **run shell commands** (the `ultron` CLI) and
**outbound HTTPS** (job boards and ATS APIs). If commands fail for network or environment reasons, run
`ultron doctor` (add `--no-network` to skip the network checks). It prints `{"ok": ..., "checks": [...]}`; every
failed check has a `fix` that points to the section of this file for the detected platform.

Hosts ULTRON talks to are public job boards and ATS APIs (for example `boards-api.greenhouse.io`), over HTTPS (port
443). Blocking DNS or HTTPS for them, or running commands in a sandbox without network, makes `fetch` and `discover`
fail. Only loosen your sandbox as far as you are comfortable: the flags below that skip every check are meant for
machines that are already isolated.

Each section lists only what could be confirmed from the tool's own `--help` output; for anything else it says to
read the tool's documentation. Re-check after upgrading the tool.

## Claude Code

- Shell commands: Claude Code asks permission before running a command. Allow `ultron` commands for the session or
  permanently from the permission prompt, or start with `--allowedTools` (the help describes a list of tool names with
  optional command patterns; see your tool's documentation for the exact pattern syntax for shell commands, and use the
  `ultron` executable path from `RUNTIME.md`).
- `--permission-mode <mode>` selects how much is asked (`acceptEdits`, `auto`, `bypassPermissions`, `manual`,
  `dontAsk`, `plan`).
- `--dangerously-skip-permissions` bypasses all checks; the help recommends it only for sandboxes with no internet
  access, so avoid it on a normal machine.
- Network: if your Claude Code setup runs commands in a sandbox, see your tool's documentation for sandbox/network
  settings and allow the hosts that `ultron doctor` reports as unreachable.
- Detected by the environment variable `CLAUDECODE`.

verified on 2026-11-16 via `claude --help` (flags only; sandbox network settings not covered by `--help`).

## Codex

- Shell commands run inside Codex's sandbox. `-s, --sandbox <mode>` accepts `read-only`, `workspace-write` and
  `danger-full-access`; `read-only` cannot run ULTRON (it writes to `ULTRON_HOME`). Under `workspace-write`, writes
  are limited to the workspace, so `ULTRON_HOME` (default `~/.ultron`) must be writable by an explicit setting or by
  pointing `ULTRON_HOME` inside the workspace.
- `-a, --ask-for-approval <policy>` (`on-request` or `never`) controls when you are asked.
- `-c, --config key=value` overrides a setting from `~/.codex/config.toml` for one run.
- `--dangerously-bypass-approvals-and-sandbox` removes both; the help says it is intended only for environments that
  are already externally sandboxed.
- Network: the sandbox may block outbound network by default. See your tool's documentation for sandbox/network
  settings (they live in `~/.codex/config.toml`), then re-run `ultron doctor`. In the agent smoke test (codex-cli
  0.169.0, Windows) `-s workspace-write -c sandbox_workspace_write.network_access=true` was enough for every ULTRON
  command.
- Windows: the sandbox runs commands as a separate sandbox user, so the files it creates in `ULTRON_HOME` are
  owned by that user. If you cannot open `results.md` outside Codex, keep `ULTRON_HOME` in a folder that grants your
  own user access. If Codex reports "access denied" reading the skill's own files, reinstall with the current
  `install.py`.
- Detected by `CODEX_SANDBOX` or `CODEX_HOME`. `codex doctor` checks Codex's own installation and config.

verified on 2026-11-16 via `codex --help` and `codex sandbox --help` (flags only; network settings are not covered by
`--help`); the Windows sandbox-user behaviour was observed in an agent smoke test with codex-cli 0.169.0.

## OpenCode

- Shell commands: OpenCode asks for permission per tool call. `--auto` auto-approves permissions that are not
  explicitly denied (listed in `opencode --help`); permission rules live in the OpenCode config file, see your tool's
  documentation for the permission settings.
- Network: OpenCode does not sandbox commands by itself as far as `--help` shows; if outbound HTTPS fails, the block
  is in your OS, proxy or container. See your tool's documentation for sandbox/network settings.
- Detected by the environment variable `OPENCODE`.

verified on 2026-11-16 via `opencode --help` (flags only; permission config keys not covered by `--help`).

## Antigravity

- Shell commands: the `agy` CLI asks for permission for tool calls. `--mode <accept-edits|auto|plan>` sets the
  execution mode, and `--dangerously-skip-permissions` approves everything without prompting (use only on an
  isolated machine).
- `--sandbox` runs the session "with terminal restrictions enabled"; do not use it for ULTRON runs, or allow network
  as your tool's documentation describes. `--settings` takes a JSON object of `settings.json` keys for one session.
- Network: see your tool's documentation for sandbox/network settings.
- Detected by environment variables starting with `ANTIGRAVITY`.

verified on 2026-11-16 via `agy --help` (flags only; settings keys not covered by `--help`).
