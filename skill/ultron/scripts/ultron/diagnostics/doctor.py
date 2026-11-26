"""``ultron doctor``: read-only environment, installation and network checks."""

from __future__ import annotations

import importlib.util
import os
import socket
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

import ultron
from ultron.infra import installs
from ultron.infra.net import HttpClient
from ultron.infra.platforms import (
    PLATFORM_LABELS,
    PLATFORM_NAMES,
    detect_platforms,
    global_skill_dirs,
    project_skill_dirs,
)
from ultron.infra.searxng import docker_available, json_health
from ultron.stages import http_factory
from ultron.storage import ledger

MIN_PYTHON = (3, 12)
EXTRAS = (("pypdf", "cv"), ("openpyxl", "xlsx"))
NETWORK_HOST = "boards-api.greenhouse.io"
NETWORK_URL = f"https://{NETWORK_HOST}/v1/boards/greenhouse/jobs?content=false"
NETWORK_TIMEOUT = 10.0
PLATFORMS_DOC = "references/platforms.md"
PUBLIC_CHECK_IDS = frozenset({
    "python_version", "ultron_home_writable", "db_schema", "optional_extras",
    "skill_installed", "network_dns", "network_https",
})


@dataclass
class Check:
    """Result record of a single ``ultron doctor`` environment check."""
    id: str
    ok: bool
    detail: str
    fix: str = ""


# -- individual checks ---------------------------------------------------------------------------------


def check_python() -> Check:
    """Verify the running Python interpreter meets the minimum supported version."""
    version = ".".join(str(p) for p in sys.version_info[:3])
    if sys.version_info >= MIN_PYTHON:
        return Check("python_version", True, f"Python {version}")
    return Check(
        "python_version",
        False,
        f"Python {version}; ULTRON needs 3.12 or newer",
        "Install Python 3.12+ and re-run install.py with it (https://www.python.org/downloads/).",
    )


def check_home_writable() -> Check:
    """Verify ``ULTRON_HOME`` (or its nearest existing ancestor) is writable."""
    home = installs.home_path()
    probe = home
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        with tempfile.TemporaryFile(dir=probe):
            pass
    except OSError as exc:
        return Check(
            "ultron_home_writable",
            False,
            f"{home} is not writable ({exc.__class__.__name__}: {exc})",
            "Make the folder writable, or set ULTRON_HOME to a folder you can write to "
            f"(your agent's sandbox may need to allow writes there; see {PLATFORMS_DOC}).",
        )
    note = "" if home.exists() else " (does not exist yet; it will be created)"
    return Check("ultron_home_writable", True, f"{home}{note}")


def check_db_schema() -> Check:
    """Check whether the SQLite ledger exists and matches ``SCHEMA_VERSION``."""
    path = installs.home_path() / "ultron.sqlite3"
    expected = ledger.SCHEMA_VERSION
    if not path.exists():
        return Check("db_schema", True, f"ledger not created yet (it will be created at schema v{expected})")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check(
            "db_schema",
            False,
            f"cannot read {path}: {exc}",
            "Run `ultron data check`; restore a backup with `ultron data restore` if the file is damaged.",
        )
    if version == expected:
        return Check("db_schema", True, f"schema v{version}")
    if version < expected:
        return Check("db_schema", True, f"schema v{version}; it migrates to v{expected} (with backup) on next use")
    return Check(
        "db_schema",
        False,
        f"schema v{version} is newer than this ULTRON (v{expected})",
        "Update ULTRON (python install.py --update) so it matches the version that wrote the ledger.",
    )


def check_extras() -> Check:
    """Verify optional dependencies (``pypdf`` and ``openpyxl``) are importable."""
    missing = [(mod, extra) for mod, extra in EXTRAS if importlib.util.find_spec(mod) is None]
    if not missing:
        return Check("optional_extras", True, "pypdf and openpyxl available")
    names = ", ".join(mod for mod, _ in missing)
    extras = ",".join(extra for _, extra in missing)
    return Check(
        "optional_extras",
        False,
        f"missing: {names} (CV extraction / Excel export need them)",
        f"pip install 'ultron[{extras}]' in ULTRON's venv, or re-run `python install.py --update`.",
    )


def _skill_state(path: Path) -> str:
    if path.is_symlink() or (os.name == "nt" and path.is_junction()):
        return "installed (dev link)" if installs.registered_link(path) else "foreign"
    if not path.exists():
        return "missing"
    return "installed" if path.is_dir() and installs.marker_valid(path) else "foreign"


def check_skill_installed(home: Path | None = None, cwd: Path | None = None) -> Check:
    """Global installs of every platform plus project installs in the current folder.

    ok when at least one is installed. With none installed, a dev checkout is informational (ok, with install
    advice in ``detail``); anywhere else it fails.
    """
    states = {f"global {n}": _skill_state(p) for n, p in global_skill_dirs(home or Path.home()).items()}
    states.update({n: _skill_state(p) for n, p in project_skill_dirs(cwd or Path.cwd()).items()})
    summary = "; ".join(f"{name}: {state}" for name, state in states.items())
    if any(s.startswith("installed") for s in states.values()):
        return Check("skill_installed", True, summary)
    advice = "Run `python install.py --global` (all platforms) or `python install.py --project DIR` from the ULTRON repo."
    foreign = [n for n, s in states.items() if s == "foreign"]
    if foreign:
        advice = (
            f"A folder not created by ULTRON's installer exists for: {', '.join(foreign)}. "
            "Move it away, then run `python install.py --global` or `--project DIR`."
        )
    if installs.in_dev_checkout():
        return Check("skill_installed", True, f"{summary}. Running from a source checkout; no agent has the skill yet.", advice)
    return Check("skill_installed", False, summary, advice)


def _network_fix(env: Mapping[str, str]) -> str:
    detected = detect_platforms(env)
    names = detected or list(PLATFORM_NAMES)
    sections = ", ".join(f"'{PLATFORM_LABELS[n]}'" for n in names)
    lead = "Your agent's sandbox or permissions probably block network access."
    if detected:
        return f"{lead} See {PLATFORMS_DOC}, section {sections}."
    return f"{lead} Platform not detected; see {PLATFORMS_DOC}, sections {sections} (use yours)."


def _default_resolver(hostname: str, port: int) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)]


def check_network_dns(env: Mapping[str, str], resolver: Callable | None = None) -> Check:
    """Check DNS resolution for external connectivity tests."""
    try:
        addresses = list((resolver or _default_resolver)(NETWORK_HOST, 443))
    except OSError as exc:
        return Check("network_dns", False, f"cannot resolve {NETWORK_HOST} ({exc.__class__.__name__})", _network_fix(env))
    if not addresses:
        return Check("network_dns", False, f"no addresses for {NETWORK_HOST}", _network_fix(env))
    return Check("network_dns", True, f"{NETWORK_HOST} resolves")


def check_network_https(env: Mapping[str, str]) -> Check:
    """Check outbound HTTPS connectivity within tight request budgets."""
    options = {
        "max_requests": 2,
        "max_requests_per_host": 2,
        "timeout": NETWORK_TIMEOUT,
        "min_interval_per_host": 0.0,
    }
    options.update(http_factory.TEST_OVERRIDES)
    try:
        with HttpClient(**options) as client:
            client.request_json(NETWORK_URL, timeout=NETWORK_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - any failure (policy, HTTP, transport) is a failed check, not a crash
        return Check("network_https", False, f"GET {NETWORK_HOST} failed: {exc}", _network_fix(env))
    return Check("network_https", True, f"GET {NETWORK_HOST} ok")


def check_web_search(*, network: bool) -> Check:
    """Report the best available keyless search path without failing doctor."""
    if network and json_health():
        return Check("web_search", True, "SearXNG JSON is available.")
    fix = "Run ultron searxng up to enable the local service." if docker_available() else None
    return Check("web_search", True, "Built-in DuckDuckGo HTML search is available.", fix)


def run_checks(*, network: bool = True) -> list[Check]:
    """Run environment, storage, installation, and optional network checks."""
    checks = [check_python(), check_home_writable(), check_db_schema(), check_extras(), check_skill_installed(),
              check_web_search(network=network)]
    if network:
        checks.append(check_network_dns(os.environ, http_factory.TEST_OVERRIDES.get("resolver")))
        checks.append(check_network_https(os.environ))
    return checks


def to_payload(checks: list[Check]) -> dict:
    """Convert doctor ``Check`` items into the CLI JSON output payload."""
    return {"ok": all(c.ok for c in checks), "checks": [asdict(c) for c in checks]}


def to_public_payload(checks: list[Check]) -> dict:
    """Return only build information and statuses for known checks."""
    return {
        "ok": all(c.ok for c in checks),
        "ultron": ultron.__version__,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        "os": {"win32": "Windows", "darwin": "macOS", "linux": "Linux"}.get(sys.platform, "Other"),
        "checks": [{"id": c.id, "ok": bool(c.ok)} for c in checks if c.id in PUBLIC_CHECK_IDS],
    }
