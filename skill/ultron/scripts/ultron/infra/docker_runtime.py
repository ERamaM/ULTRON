"""Host-specific Docker install guidance and noninteractive local daemon startup."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def install_hint() -> str:
    """Explain how to install and start Docker on the running OS."""
    if sys.platform == "win32":
        return ("Run winget install Docker.DockerDesktop, or install Docker Desktop for Windows. "
                "Enable WSL 2 if prompted and restart Windows if required. Open Docker Desktop and wait for the engine.")
    if sys.platform == "darwin":
        return ("Run brew install --cask docker, or install Docker Desktop for macOS. "
                "Open Docker Desktop and finish its first-run setup.")
    return ("Install Docker Engine using your Linux distribution's instructions and start the docker service "
            "(for example, sudo systemctl enable --now docker). Verify that docker info works for your user.")


def start_hint() -> str:
    """Explain how to finish starting a daemon that is unavailable."""
    if sys.platform in {"win32", "darwin"}:
        return "Open Docker Desktop, finish any first-run setup, and check that the Docker engine is running."
    return "Start the Docker service and verify your user can run docker info."


def _windows_locations() -> list[Path]:
    candidates = []
    if root := os.environ.get("ProgramFiles"):
        candidates.append(Path(root) / "Docker" / "Docker")
    if root := os.environ.get("LOCALAPPDATA"):
        programs = Path(root) / "Programs"
        candidates.extend((programs / "DockerDesktop", programs / "Docker" / "Docker"))
    return [path for path in candidates if path.is_absolute()]


def docker_executable() -> str | None:
    """Locate the CLI on PATH or in a standard Desktop installation."""
    if executable := shutil.which("docker"):
        return executable
    candidates = []
    if sys.platform == "win32":
        candidates = [path / "resources" / "bin" / "docker.exe" for path in _windows_locations()]
    elif sys.platform == "darwin":
        candidates = [Path("/Applications/Docker.app/Contents/Resources/bin/docker")]
    return next((str(path) for path in candidates if path.is_file()), None)


def _desktop_command() -> list[str]:
    if sys.platform == "darwin":
        return ["/usr/bin/open", "-a", "Docker"]
    for path in _windows_locations():
        exe = path / "Docker Desktop.exe"
        if exe.is_file():
            return [str(exe)]
    return []


def start_daemon(endpoint: str, *, timeout: float) -> bool:
    """Request startup of installed local Docker software without prompts or privilege escalation."""
    if sys.platform in {"win32", "darwin"}:
        command = _desktop_command()
        if not command:
            return False
        try:
            subprocess.Popen(
                command, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
            )
            return True
        except OSError:
            return False
    if not sys.platform.startswith("linux"):
        return False
    desktop = endpoint.endswith("/.docker/desktop/docker.sock")
    user_service = desktop or endpoint.startswith("unix:///run/user/")
    command = ["systemctl", *(["--user"] if user_service else []), "--no-ask-password", "--no-block",
               "start", "docker-desktop" if desktop else "docker"]
    try:
        return subprocess.run(command, shell=False, check=False, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
