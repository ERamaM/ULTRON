"""``ultron update``: check for a stable release and prepare (never run) the installer that applies it.

The running installation cannot replace itself (on Windows the venv's ``ultron.exe`` is locked while it runs), so
``prepare_update`` only downloads and verifies the release's source archive and returns the ``install.py --update``
command(s) for the agent to run as a separate process. Nothing from the archive is executed here.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from ultron import __version__
from ultron.infra import installs as install_layout
from ultron.update import archive as release_archive
from ultron.update import copies as install_copies
from ultron.update import installer
from ultron.update.notice import format_version, latest_release, parse_version, release_for_version, repository

API_TIMEOUT = 10.0


class UpdateError(RuntimeError):
    """A failure with its JSON error code and exit code."""

    def __init__(self, error: str, detail: str, code: int = 1) -> None:
        super().__init__(detail)
        self.error = error
        self.code = code



# ----------------------------------------------------------------------------------------------- check


def check() -> dict:
    """Installed vs latest stable release. Raises ``ReleaseLookupError`` / ``InvalidRepository``."""
    release = latest_release(timeout=API_TIMEOUT)
    installed = parse_version(__version__)
    available = release is not None and (installed is None or release["version"] > installed)
    return {
        "installed": __version__,
        "latest": format_version(release["version"]) if release else None,
        "update_available": available,
        "release_url": release["url"] if release else None,
    }


# --------------------------------------------------------------------------------------------- prepare

NEXT_STEPS = [
    (
        "Ask the user before running anything. If they agree, run each command in `commands` as its own separate "
        "process, one after another, exactly as given (`argv`, with `env` when present). Run them from a shell "
        "where ULTRON's venv is not activated (unset VIRTUAL_ENV); the commands already avoid using it."
    ),
    "Then run `ultron doctor` with the executable listed in RUNTIME.md and report any failed check.",
    "Tell the user to restart the agent session so the updated skill is loaded.",
    "The downloaded source in `cleanup` is no longer needed once the installer has finished; it may be deleted.",
]


def prepare_update(*, to: tuple[int, int, int] | None = None, projects: list[Path] | None = None) -> dict:
    """Resolve the target release, find the installs, download and verify the source, build the commands."""
    projects = list(projects or [])
    if to is not None:
        release = release_for_version(to, timeout=API_TIMEOUT)
        if release is None:
            raise UpdateError("release_not_found", f"v{format_version(to)} is not a published stable release", code=2)
    else:
        release = latest_release(timeout=API_TIMEOUT)
    installed = parse_version(__version__)
    result: dict = {"action": "update", "installed": __version__, "commands": []}
    if release is None:
        result.update(status="no_release", detail="no stable release is published")
        return result
    target = release["version"]
    result.update(target=format_version(target), tag=release["tag"], release_url=release["url"])
    if installed is not None and to is None and target < installed:
        # The CLI is ahead of the latest release: any installer run would move the shared venv back.
        result.update(status="up_to_date", detail=f"ULTRON {__version__} is newer than the latest release")
        return result
    reason = "cli_outdated"
    if installed is not None and target < installed:
        result["downgrade"] = True
        reason = "requested_version"
    home, cwd = Path.home(), Path.cwd()
    copies = install_copies.find_copies(home=home, cwd=cwd, projects=projects)
    if pruned := install_copies.missing_records():
        result["pruned_records"] = pruned
    if installed == target:
        # The CLI is current; skill copies that an older installer left behind still need the update.
        copies = [c for c in copies if (v := parse_version(c["version"])) is not None and v < target]
        if not copies:
            result.update(status="up_to_date", detail=f"ULTRON {__version__} is already up to date")
            return result
        reason = "stale_skill_copies"
    if install_layout.in_dev_checkout():
        result.update(status="source_checkout",
                      detail="running from a source checkout: update it with git, then run install.py --dev --update")
        return result
    installs, manual = install_copies.split_mixed_antigravity(install_copies.group_scopes(copies), home)
    if manual:
        result["manual"] = manual
    links = install_copies.dev_links(home=home, cwd=cwd, projects=projects)
    if links:
        result["dev_links"] = links
        result["dev_note"] = "linked (--dev) skill folders follow their checkout: update it with git instead"
    if not installs:
        result.update(status="no_installs", detail="no ULTRON skill installed by install.py was found (installs.json, "
                      "global folders, the current folder or --project folders)")
        return result
    result["reason"] = reason
    url, _, _ = repository()
    temp = Path(tempfile.mkdtemp(prefix="ultron-update-"))
    try:
        archive = temp / "source.zip"
        release_archive.download_archive(f"{url}/archive/refs/tags/{release['tag']}.zip", archive)
        (temp / "src").mkdir()
        source = release_archive.extract_source(archive, temp / "src", expected_version=format_version(target))
        archive.unlink()
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    result.update(
        status="ready",
        source=str(source),
        cleanup=str(temp),
        installs=installs,
        commands=installer.installer_commands(source / "install.py", installs),
        next_steps=NEXT_STEPS,
    )
    if env := installer.installer_env():
        result["env"] = env
    return result
