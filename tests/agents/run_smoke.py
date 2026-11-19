"""Smoke test of the ULTRON skill with a real coding agent, run headless.

Usage::

    python tests/agents/run_smoke.py --platform {claude,codex,opencode,antigravity} [--keep]

What it does:

1. Creates a temporary folder with ``project/`` (the agent's working directory) and
   ``project/.ultron-home`` (``ULTRON_HOME``, inside the project so sandboxed agents can write it).
2. Runs ``install.py --project project`` with that ``ULTRON_HOME``.
3. Runs the agent non-interactively in ``project/`` with the prompt in ``smoke_prompt.md`` and an
   environment built from an allowlist (OS basics, home folders for the agents' own logins, proxies, and only
   the credentials that agent needs).
4. Validates with the installed CLI (``ultron runs list``) and the run folder: a run whose
   ``stages_done`` include seeds, discover_ats, fetch, filter and finalize; ``seeds_verified.jsonl``
   with at least one row; ``results.md``; and the run_id mentioned in the agent's output.

Prints one JSON object: ``{"platform", "version", "ok", "run_id", "failures", ...}`` (extra keys are
evidence: duration, exit code, workdir, stages, agent files). Agents never run in pytest; only
:func:`validate` and the helpers are unit-tested (``test_run_smoke_validation.py``).
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
PROMPT_FILE = HERE / "smoke_prompt.md"
PLATFORMS = ("claude", "codex", "opencode", "antigravity")
EXECUTABLES = {"claude": "claude", "codex": "codex", "opencode": "opencode", "antigravity": "agy"}
REQUIRED_STAGES = ("seeds", "discover_ats", "fetch", "filter", "finalize")
# Stages only an agent-driven loop produces (the unattended shortcut never marks them).
AGENTIC_STAGES = ("candidates", "evaluate")
DEFAULT_OPENCODE_MODEL = "opencode/glm-5.3-flash-free"
RUN_ID_RE = re.compile(r"\b\d{8}-\d{6}-[0-9a-f]{6}\b")
NO_RUN_ID = "agent output does not mention the run_id"

# Child environment = allowlist (see child_env). OS basics needed to start processes, shells, PowerShell, node and
# Python; home/profile folders (agents keep their logins there); proxy settings for networks that need them.
_BASE_ENV = (
    "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "TEMP", "TMP", "TMPDIR",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
    "PROGRAMDATA", "PSMODULEPATH", "OS", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS",
    "USERNAME", "USERDOMAIN", "COMPUTERNAME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TZ",
    "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
)
_BASE_ENV_PREFIXES = ("LC_", "XDG_")
# Credentials a platform genuinely needs from the environment. Claude Code, Codex and agy authenticate from
# their own files under the home folder (OAuth); OpenCode's Zen models read OPENCODE_API_KEY.
_PLATFORM_ENV: dict[str, tuple[str, ...]] = {"opencode": ("OPENCODE_API_KEY",)}


# --------------------------------------------------------------------------- validation (unit-tested)

def find_run_ids(text: str) -> list[str]:
    seen: list[str] = []
    for match in RUN_ID_RE.findall(text or ""):
        if match not in seen:
            seen.append(match)
    return seen


def _count_rows(path: Path) -> int:
    if not path.is_file():
        return -1
    with path.open(encoding="utf-8", errors="replace") as handle:
        return sum(1 for line in handle if line.strip())


def validate(ultron_home: Path, runs_listing: list[dict], agent_output: str) -> dict:
    """Check the run an agent produced. ``runs_listing`` is ``ultron runs list``'s ``runs`` array."""
    failures: list[str] = []
    by_id = {r.get("run_id"): r for r in runs_listing if r.get("run_id")}
    reported = [rid for rid in find_run_ids(agent_output) if rid in by_id]
    if not by_id:
        return {"ok": False, "run_id": None, "failures": ["no run found in ULTRON_HOME"],
                "evidence": {"runs_total": 0}}
    if reported:
        # The last id the agent mentions is usually the one in its final answer.
        run_id = reported[-1]
    else:
        failures.append(NO_RUN_ID)
        run_id = max(by_id, key=lambda rid: (len(set(by_id[rid].get("stages_done") or []) & set(REQUIRED_STAGES)),
                                             rid))
    run = by_id[run_id]
    stages = list(run.get("stages_done") or [])
    missing = [s for s in REQUIRED_STAGES if s not in stages]
    if missing:
        failures.append(f"run {run_id} stages_done missing {missing}")
    run_dir = Path(ultron_home) / "runs" / run_id
    seeds_rows = _count_rows(run_dir / "seeds_verified.jsonl")
    if seeds_rows < 1:
        state = "missing" if seeds_rows < 0 else "empty"
        failures.append(f"seeds_verified.jsonl {state} in run {run_id}")
    results_md = run_dir / "results.md"
    if not results_md.is_file():
        failures.append(f"results.md missing in run {run_id}")
    agent_files = sorted(p.name for p in run_dir.glob("agent_*")) if run_dir.is_dir() else []
    evidence = {
        "runs_total": len(by_id),
        "profile_slug": run.get("profile_slug"),
        "preset": run.get("preset"),
        "stages_done": stages,
        "seeds_verified_rows": max(seeds_rows, 0),
        "agent_files": agent_files,
        "agentic": bool(agent_files) or any(s in stages for s in AGENTIC_STAGES),
        "results_md": str(results_md) if results_md.is_file() else None,
    }
    return {"ok": not failures, "run_id": run_id, "failures": failures, "evidence": evidence}


def list_runs_via_cli(ultron_cmd: list[str], env_overrides: dict[str, str]) -> list[dict]:
    env = {**os.environ, **env_overrides, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.run([*ultron_cmd, "runs", "list"], capture_output=True, text=True, encoding="utf-8",
                          errors="replace", env=env, timeout=120, stdin=subprocess.DEVNULL, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"ultron runs list failed ({proc.returncode}): {proc.stdout.strip()} {proc.stderr.strip()}")
    return json.loads(proc.stdout)["runs"]


def child_env(platform: str, ultron_home: Path, base: dict[str, str] | None = None, *,
              cwd: Path | None = None) -> dict[str, str]:
    """Allowlisted environment for the agent: nothing from the parent passes unless it is listed here.

    Residual exposure (documented in the e2e report): the home/profile folders stay visible because every agent
    reads its own login from them (Claude Code ``~/.claude``, Codex ``~/.codex``, OpenCode ``~/.config`` /
    ``~/.local/share/opencode``, agy ``~/.gemini`` and ``%LOCALAPPDATA%``); an agent can therefore still see the
    user's global configuration and skills.
    """
    parent = dict(os.environ if base is None else base)
    allowed = {name.upper() for name in (*_BASE_ENV, *_PLATFORM_ENV.get(platform, ()))}
    env = {key: value for key, value in parent.items()
           if key.upper() in allowed or any(key.upper().startswith(prefix) for prefix in _BASE_ENV_PREFIXES)}
    env["ULTRON_HOME"] = str(ultron_home)
    env["PYTHONIOENCODING"] = "utf-8"
    if cwd is not None:
        # OpenCode takes its project folder from PWD (inherited from the calling shell), not from the process
        # cwd: without this it works on the folder the harness was started from.
        env["PWD"] = str(cwd)
    return env


# --------------------------------------------------------------------------- agent commands

def _exe(platform: str) -> str:
    name = EXECUTABLES[platform]
    return shutil.which(name) or name


def build_command(platform: str, prompt: str, project: Path, *, model: str | None) -> list[str]:
    """Command line for one headless agent run (claude and codex read the prompt on stdin)."""
    exe = _exe(platform)
    if platform == "claude":
        # Only project/local settings (no user plugins or hooks), no MCP servers, no saved session; tools
        # pre-approved explicitly, anything else is denied instead of prompting.
        cmd = [exe, "-p", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config",
               "--setting-sources", "project,local", "--permission-mode", "dontAsk",
               "--allowedTools", "Bash,PowerShell,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,Skill"]
        return cmd + (["--model", model] if model else [])
    if platform == "codex":
        # workspace-write sandbox with outbound network; ULTRON_HOME lives inside the workspace. "-" = stdin.
        # --ignore-user-config: no user hooks/MCP servers (auth still comes from CODEX_HOME).
        cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "-s", "workspace-write",
               "-c", "sandbox_workspace_write.network_access=true", "-C", str(project)]
        if os.name == "nt":
            # Without the user config Codex falls back to read-only on Windows unless its sandbox is named.
            cmd += ["-c", 'windows.sandbox="elevated"']
        cmd += ["-c", 'model_reasoning_effort="medium"']  # the user-config default; without it: none
        return cmd + (["-m", model] if model else []) + ["-"]
    if platform == "opencode":
        return [exe, "run", "--standalone", "--auto", "--archived", "--title", "ultron-smoke",
                "-m", model or DEFAULT_OPENCODE_MODEL, prompt]
    if platform == "antigravity":
        cmd = [exe, "--print", prompt, "--dangerously-skip-permissions", "--print-timeout", "0"]
        return cmd + (["--model", model] if model else [])
    raise ValueError(f"unknown platform {platform!r}")


def prompt_on_stdin(platform: str) -> bool:
    return platform in ("claude", "codex")


def tool_version(platform: str) -> str:
    try:
        proc = subprocess.run([_exe(platform), "--version"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60, stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"
    return (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr).strip() else "?"


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def run_agent(cmd: list[str], *, cwd: Path, env: dict[str, str], stdin_text: str | None, log: Path,
              timeout_s: float) -> tuple[int | None, bool]:
    """Run the agent, streaming stdout+stderr into ``log``. Returns ``(exit_code, timed_out)``."""
    kwargs: dict = {"cwd": cwd, "env": env, "stderr": subprocess.STDOUT}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    with log.open("wb") as out:
        kwargs["stdout"] = out
        kwargs["stdin"] = subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL
        proc = subprocess.Popen(cmd, **kwargs)
        if stdin_text is not None:
            proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        try:
            return proc.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.wait(timeout=60)
            return None, True


def _grant_current_user(root: Path) -> None:
    """Windows: give the current user explicit, inherited full control of ``root``.

    ``tempfile.mkdtemp`` gives ``root`` a private ACL (SYSTEM, Administrators and OWNER RIGHTS only). Codex's
    elevated Windows sandbox runs commands as a separate sandbox user, so folders it creates are owned by that user
    and the real user could not read the run (seen with codex-cli 0.169.0). An explicit inherited entry keeps
    everything readable for validation and cleanup.
    """
    if os.name != "nt":
        return
    user = subprocess.run(["whoami"], capture_output=True, text=True, check=False).stdout.strip()
    if user:
        subprocess.run(["icacls", str(root), "/grant", f"{user}:(OI)(CI)F", "/Q"], capture_output=True, check=False)


def _ultron_exe(ultron_home: Path) -> Path:
    sub = ("Scripts", "ultron.exe") if os.name == "nt" else ("bin", "ultron")
    return ultron_home.joinpath("venv", *sub)


def smoke(platform: str, *, keep: bool, timeout_min: float, model: str | None, workroot: Path | None) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"ultron-smoke-{platform}-", dir=workroot))
    project = root / "project"
    home = project / ".ultron-home"
    project.mkdir()
    _grant_current_user(root)
    result: dict = {"platform": platform, "version": tool_version(platform), "ok": False, "run_id": None,
                    "failures": [], "workdir": str(root)}
    started = time.monotonic()
    try:
        env = child_env(platform, home, cwd=project)
        inst = subprocess.run([sys.executable, str(REPO / "install.py"), "--project", str(project)],
                              capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
                              timeout=900, stdin=subprocess.DEVNULL, check=False)
        (root / "install.log").write_text(inst.stdout + "\n" + inst.stderr, encoding="utf-8")
        if inst.returncode != 0:
            result["failures"].append(f"install.py failed with exit {inst.returncode} (see install.log)")
            return result
        prompt = PROMPT_FILE.read_text(encoding="utf-8").strip()
        cmd = build_command(platform, prompt, project, model=model)
        result["command"] = [c if c != prompt else "<smoke_prompt.md>" for c in cmd]
        log = root / "agent.log"
        t0 = time.monotonic()
        code, timed_out = run_agent(cmd, cwd=project, env=env, stdin_text=prompt if prompt_on_stdin(platform) else None,
                                    log=log, timeout_s=timeout_min * 60)
        result["agent_seconds"] = round(time.monotonic() - t0)
        result["exit_code"] = code
        output = log.read_text(encoding="utf-8", errors="replace")
        if timed_out:
            result["failures"].append(f"agent timed out after {timeout_min:g} min")
        elif code != 0:
            result["failures"].append(f"agent exited with code {code}")
        listing = list_runs_via_cli([str(_ultron_exe(home))], {"ULTRON_HOME": str(home)})
        (root / "runs_list.json").write_text(json.dumps(listing, indent=2), encoding="utf-8")
        checked = validate(home, listing, output)
        result["run_id"] = checked["run_id"]
        result["failures"].extend(checked["failures"])
        result["evidence"] = checked["evidence"]
        result["ok"] = not result["failures"]
        return result
    except Exception as exc:  # noqa: BLE001 - report any harness error as a failure in the JSON
        result["failures"].append(f"harness error: {type(exc).__name__}: {exc}")
        return result
    finally:
        result["duration_s"] = round(time.monotonic() - started)
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
            result["workdir"] = None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--platform", required=True, choices=PLATFORMS)
    parser.add_argument("--keep", action="store_true", help="keep the temporary folder (logs, ULTRON_HOME)")
    parser.add_argument("--timeout-min", type=float, default=45.0)
    parser.add_argument("--model", default=None, help=f"model override (opencode default: {DEFAULT_OPENCODE_MODEL})")
    parser.add_argument("--workroot", type=Path, default=None, help="parent folder for the temporary folder")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = smoke(args.platform, keep=args.keep, timeout_min=args.timeout_min, model=args.model,
                   workroot=args.workroot)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
