# Getting Started

## Installation

ULTRON installs its CLI into an isolated virtual environment under `$ULTRON_HOME/venv` (default `~/.ultron/venv`) so it never touches your global Python or `PATH`, and installs skill copies for every detected agent platform.

### Zero-Python Bootstrap (Recommended)

No pre-installed Python is needed. If Python 3.12+ (with `venv` / `ensurepip`) is absent, the installer automatically provisions standalone [`uv`](https://docs.astral.sh/uv/) and a managed Python 3.12 runtime without requiring administrator or `sudo` privileges:

- **macOS / Linux:**
  ```sh
  curl -fsSL https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.sh | sh
  ```
- **Windows (PowerShell):**
  ```powershell
  irm https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.ps1 | iex
  ```

From a local clone, run `./install.sh` (macOS/Linux) or `powershell -ExecutionPolicy Bypass -File .\install.ps1` (Windows).

### Manual Install From a Clone (Python 3.12+)

```bash
python install.py
```

On Linux/macOS use `python3 install.py` (on Debian/Ubuntu without `uv`, install `python3-venv` first). Both the bootstrap scripts and `install.py` enable the `cv` (`pypdf`) and `xlsx` (`openpyxl`) extras by default and write `RUNTIME.md` next to `SKILL.md` inside each installed skill folder. Restart your agent or start a new session after installing.

### Installer Options

| Option | Effect |
| --- | --- |
| `--global` | Install into user-level agent skill folders (default) |
| `--project DIR` | Install into project folders (`DIR/.claude` and `DIR/.agents`) |
| `--platforms LIST` | Comma-separated list: `claude`, `codex`, `opencode`, `antigravity` |
| `--dev` / `--copy` | Install in editable mode (symlinks the skill unless `--copy` is passed) |
| `--update` | Refresh the virtual environment and existing ULTRON skill installs |
| `--uninstall` | Remove ULTRON-managed skill installs (keeps profiles and job history) |
| `--purge-data` | Combine with `--uninstall` to also delete ULTRON data inside `$ULTRON_HOME` |
| `--dry-run` | Preview installer actions without changing the venv, skills, or data |

## Verify the Installation

Run `doctor` with the venv executable directly (or ask your agent to run `ultron doctor`):

- **Windows PowerShell:** `& "$HOME\.ultron\venv\Scripts\ultron.exe" doctor`
- **Windows CMD:** `"%USERPROFILE%\.ultron\venv\Scripts\ultron.exe" doctor`
- **macOS / Linux:** `~/.ultron/venv/bin/ultron doctor`

Useful flags:

- `--no-network`: skip live DNS and HTTPS connectivity checks against public ATS hosts.
- `--public`: omit local filesystem paths and raw details when sharing diagnostics.

## Agent Platform & Sandbox Setup

ULTRON requires permission to **execute local shell commands** (`ultron`) and **outbound HTTPS (port 443)** to query public ATS boards (`boards-api.greenhouse.io`, `jobs.lever.co`, etc.).

| Agent | Invoke Syntax | Global Skill Path | Key Setup Notes |
| --- | --- | --- | --- |
| **Claude Code** | `/ultron ...` | `~/.claude/skills/ultron` | Approve `ultron` commands or pass `--allowedTools`; if sandboxed, permit outbound HTTPS to ATS hosts reported by `ultron doctor`. |
| **Codex** | `$ultron ...` | `~/.agents/skills/ultron` | `read-only` sandboxes block `$ULTRON_HOME` writes; use `-s workspace-write -c sandbox_workspace_write.network_access=true` (or point `ULTRON_HOME` inside a writable path). On Windows, Codex may write files under its sandbox user unless `ULTRON_HOME` grants your user access. |
| **OpenCode** | `ultron ...` | `~/.config/opencode/skills/ultron` | Approve shell calls per prompt or configure auto-approve rules (`--auto`). Outbound HTTPS depends only on host/OS firewall/proxy. |
| **Antigravity** | `ultron ...` | `~/.gemini/config/skills/ultron` | Use standard execution approval (`agy --mode accept-edits` or prompt confirmation); outbound HTTPS follows host OS/proxy settings. |

See [references/platforms.md](../../skill/ultron/references/platforms.md) for verified flag details per CLI.

## Updating ULTRON

After an interactive search, ULTRON checks once every 24 hours whether a newer stable GitHub release exists and displays a single notice. Set `ULTRON_DISABLE_UPDATE_CHECK=1` to disable the reminder.

When you confirm an update with your agent:

1. The agent runs `ultron update` (or `ultron update --to vX.Y.Z` for a specific stable tag; use `ultron update --check` to inspect without downloading).
2. `ultron update` downloads the release into a temporary folder and outputs the `install.py --update` commands for every recorded global and project installation in `$ULTRON_HOME/installs.json` (pass `--project DIR` for unrecorded project installs).
3. Your agent runs each installer command as a separate process, runs `ultron doctor`, and reminds you to start a new session so the refreshed skill loads. Your profiles, saved searches, job ledger, and run history remain untouched.

To update manually:

- **macOS / Linux (local clone):** `./install.sh --update`
- **macOS / Linux (web bootstrap):** `curl -fsSL https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.sh | sh -s -- --update`
- **Windows PowerShell (local clone):** `powershell -ExecutionPolicy Bypass -File .\install.ps1 --update`
- **Windows PowerShell (web bootstrap):** `& ([scriptblock]::Create((irm https://raw.githubusercontent.com/ERamaM/ULTRON/main/install.ps1))) --update`

Next: [Search Profiles](search-profiles.md).
